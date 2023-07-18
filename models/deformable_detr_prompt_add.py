# ------------------------------------------------------------------------
# Modified by Wei-Jie Huang
# ------------------------------------------------------------------------
# Deformable DETR
# Copyright (c) 2020 SenseTime. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 [see LICENSE for details]
# ------------------------------------------------------------------------
# Modified from DETR (https://github.com/facebookresearch/detr)
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
# ------------------------------------------------------------------------

"""
Deformable DETR model and criterion classes.
"""
import torch
import torch.nn.functional as F
from torch import nn
import math

from util import box_ops
from util.misc import (NestedTensor, nested_tensor_from_tensor_list,
                       accuracy, get_world_size, interpolate,
                       is_dist_avail_and_initialized, inverse_sigmoid)

from .backbone import build_backbone
from .matcher import build_matcher
from .segmentation import (DETRsegm, PostProcessPanoptic, PostProcessSegm,
                           dice_loss, sigmoid_focal_loss)
from .deformable_transformer_prompt_add import build_deforamble_transformer
from .utils import GradientReversal, init_parameter_list
import copy


def _get_clones(module, N):
    return nn.ModuleList([copy.deepcopy(module) for i in range(N)])


class DeformableDETR(nn.Module):
    """ This is the Deformable DETR module that performs object detection """
    def __init__(self, backbone, transformer, num_classes, num_queries, num_feature_levels,
                 aux_loss=True, with_box_refine=False, two_stage=False,
                 backbone_align=False, space_align=False, channel_align=False, instance_align=False,
                 prompt_modules=[], prompt_project=False, deep_prompt=False, deep_shared_prompt=False,
                 num_prompt_tokens=None, prompt_dropout_rate=0.0, prompt_init_a=-0.1, prompt_init_b=0.1,
                 prompt_domain_type='same'):
        """ Initializes the model.
        Parameters:
            backbone: torch module of the backbone to be used. See backbone.py
            transformer: torch module of the transformer architecture. See transformer.py
            num_classes: number of object classes
            num_queries: number of object queries, ie detection slot. This is the maximal number of objects
                         DETR can detect in a single image. For COCO, we recommend 100 queries.
            aux_loss: True if auxiliary decoding losses (loss at each decoder layer) are to be used.
            with_box_refine: iterative bounding box refinement
            two_stage: two-stage Deformable DETR
        """
        super().__init__()
        self.num_queries = num_queries
        self.transformer = transformer
        hidden_dim = transformer.d_model
        self.class_embed = nn.Linear(hidden_dim, num_classes)
        self.bbox_embed = MLP(hidden_dim, hidden_dim, 4, 3)
        self.num_feature_levels = num_feature_levels
        if not two_stage:
            self.query_embed = nn.Embedding(num_queries, hidden_dim*2)
        if num_feature_levels > 1:
            num_backbone_outs = len(backbone.strides)
            input_proj_list = []
            for _ in range(num_backbone_outs):
                in_channels = backbone.num_channels[_]
                input_proj_list.append(nn.Sequential(
                    nn.Conv2d(in_channels, hidden_dim, kernel_size=1),
                    nn.GroupNorm(32, hidden_dim),
                ))
            for _ in range(num_feature_levels - num_backbone_outs):
                input_proj_list.append(nn.Sequential(
                    nn.Conv2d(in_channels, hidden_dim, kernel_size=3, stride=2, padding=1),
                    nn.GroupNorm(32, hidden_dim),
                ))
                in_channels = hidden_dim
            self.input_proj = nn.ModuleList(input_proj_list)
        else:
            self.input_proj = nn.ModuleList([
                nn.Sequential(
                    nn.Conv2d(backbone.num_channels[0], hidden_dim, kernel_size=1),
                    nn.GroupNorm(32, hidden_dim),
                )])
        self.backbone = backbone
        self.aux_loss = aux_loss
        self.with_box_refine = with_box_refine
        self.two_stage = two_stage
        self.uda = backbone_align or space_align or channel_align or instance_align
        self.backbone_align = backbone_align
        self.space_align = space_align
        self.channel_align = channel_align
        self.instance_align = instance_align
        self.prompt_modules = prompt_modules
        self.prompt_project = prompt_project
        self.deep_prompt = deep_prompt
        self.deep_shared_prompt = deep_shared_prompt
        self.num_prompt_tokens = num_prompt_tokens
        self.prompt_dropout_rate = prompt_dropout_rate
        self.prompt_init_a = prompt_init_a
        self.prompt_init_b = prompt_init_b
        self.prompt_domain_type = prompt_domain_type

        prior_prob = 0.01
        bias_value = -math.log((1 - prior_prob) / prior_prob)
        self.class_embed.bias.data = torch.ones(num_classes) * bias_value
        nn.init.constant_(self.bbox_embed.layers[-1].weight.data, 0)
        nn.init.constant_(self.bbox_embed.layers[-1].bias.data, 0)
        for proj in self.input_proj:
            nn.init.xavier_uniform_(proj[0].weight, gain=1)
            nn.init.constant_(proj[0].bias, 0)

        # if two-stage, the last class_embed and bbox_embed is for region proposal generation
        num_pred = (transformer.decoder.num_layers + 1) if two_stage else transformer.decoder.num_layers
        if with_box_refine:
            self.class_embed = _get_clones(self.class_embed, num_pred)
            self.bbox_embed = _get_clones(self.bbox_embed, num_pred)
            nn.init.constant_(self.bbox_embed[0].layers[-1].bias.data[2:], -2.0)
            # hack implementation for iterative bounding box refinement
            self.transformer.decoder.bbox_embed = self.bbox_embed
        else:
            nn.init.constant_(self.bbox_embed.layers[-1].bias.data[2:], -2.0)
            self.class_embed = nn.ModuleList([self.class_embed for _ in range(num_pred)])
            self.bbox_embed = nn.ModuleList([self.bbox_embed for _ in range(num_pred)])
            self.transformer.decoder.bbox_embed = None
        if two_stage:
            # hack implementation for two-stage
            self.transformer.decoder.class_embed = self.class_embed
            for box_embed in self.bbox_embed:
                nn.init.constant_(box_embed.layers[-1].bias.data[2:], 0.0)
        if backbone_align:
            self.grl = GradientReversal()
            self.backbone_D = MLP(hidden_dim, hidden_dim, 1, 3)
            for layer in self.backbone_D.layers:
                nn.init.xavier_uniform_(layer.weight, gain=1)
                nn.init.constant_(layer.bias, 0)
        if space_align:
            self.space_D = MLP(hidden_dim, hidden_dim, 1, 3)
            for layer in self.space_D.layers:
                nn.init.xavier_uniform_(layer.weight, gain=1)
                nn.init.constant_(layer.bias, 0)
        if channel_align:
            self.channel_D = MLP(hidden_dim, hidden_dim, 1, 3)
            for layer in self.channel_D.layers:
                nn.init.xavier_uniform_(layer.weight, gain=1)
                nn.init.constant_(layer.bias, 0)
        if instance_align:
            self.instance_D = MLP(hidden_dim, hidden_dim, 1, 3)
            for layer in self.instance_D.layers:
                nn.init.xavier_uniform_(layer.weight, gain=1)
                nn.init.constant_(layer.bias, 0)

        # 0-th feature level: w=146, h=167
        # 1-th feature level: w=73, h=84
        # 2-th feature level: w=37, h=42
        # 3-th feature level: w=19, h=21
        feature_level_shapes = [
            (200, 200),
            (100, 100),
            (50, 50),
            (25, 25)
        ]

        # initialize visual prompt
        if not self.deep_prompt:
            # if prompt is shallow
            # it only needs 1 prompt to input in the 1st layer of the encoder or decoder
            if 'encoder' in self.prompt_modules:
                self.encoder_prompt_embeddings = nn.ParameterDict({
                    'src': nn.ParameterList([nn.Parameter(torch.zeros(h, w, hidden_dim)) for h, w in feature_level_shapes])
                })
            else:
                self.encoder_prompt_embeddings = None

            if 'decoder' in self.prompt_modules:
                self.decoder_prompt_embeddings = nn.ParameterDict({
                    'src': nn.Parameter(torch.zeros(num_queries, hidden_dim))
                })
            else:
                self.decoder_prompt_embeddings = None

        elif self.deep_prompt and self.deep_shared_prompt:
            # if prompt is deep shared,
            # it only needs 1 prompt for each feature levels in the encoder or decoder
            assert 'encoder' in self.prompt_modules

            self.encoder_prompt_embeddings = nn.ParameterDict({
                'src': nn.ParameterList([nn.Parameter(torch.zeros(h, w, hidden_dim)) for h, w in feature_level_shapes])
            })

            if 'decoder' in self.prompt_modules:
                self.decoder_prompt_embeddings = nn.ParameterDict({
                    'src': nn.Parameter(torch.zeros(num_queries, hidden_dim))
                })
            else:
                self.decoder_prompt_embeddings = None

        elif self.deep_prompt and not self.deep_shared_prompt:
            # if prompt is deep and not shared in layers
            # it needs 1 prompt for each feature levels and each layers
            if 'encoder' in self.prompt_modules:
                num_layers = self.transformer.encoder.num_layers
                self.encoder_prompt_embeddings = nn.ParameterDict({
                    'src': nn.ParameterList([
                        nn.ParameterList([nn.Parameter(torch.zeros(h, w, hidden_dim)) for h, w in feature_level_shapes])
                        for _ in range(num_layers)
                    ])
                })
            else:
                self.encoder_prompt_embeddings = None

            if 'decoder' in self.prompt_modules:
                num_layers = self.transformer.decoder.num_layers
                self.decoder_prompt_embeddings = nn.ParameterDict({
                    'src': nn.Parameter(torch.zeros(num_layers, num_queries, hidden_dim))
                })
            else:
                self.decoder_prompt_embeddings = None

        else:
            raise ValueError('Wrong setting of prompt')

        if self.encoder_prompt_embeddings is not None:
            init_parameter_list(self.encoder_prompt_embeddings['src'], self.prompt_init_a, self.prompt_init_b)
        if self.decoder_prompt_embeddings is not None:
            nn.init.uniform_(self.decoder_prompt_embeddings['src'].data, self.prompt_init_a, self.prompt_init_b)
        
        if self.prompt_domain_type == 'same':
            if self.encoder_prompt_embeddings is not None:
                self.encoder_prompt_embeddings['tgt'] = self.encoder_prompt_embeddings['src']
            if self.decoder_prompt_embeddings is not None:
                self.decoder_prompt_embeddings['tgt'] = self.decoder_prompt_embeddings['src']
        elif self.prompt_domain_type == 'separate':
            if self.encoder_prompt_embeddings is not None:
                self.encoder_prompt_embeddings['tgt'] = copy.deepcopy(self.encoder_prompt_embeddings['src'])
                init_parameter_list(self.encoder_prompt_embeddings['tgt'], self.prompt_init_a, self.prompt_init_b)
            if self.decoder_prompt_embeddings is not None:
                self.decoder_prompt_embeddings['tgt'] = copy.deepcopy(self.decoder_prompt_embeddings['src'])
                nn.init.uniform_(self.decoder_prompt_embeddings['tgt'].data, self.prompt_init_a, self.prompt_init_b)
        elif self.prompt_domain_type == 'inverse':
            pass
            # if self.encoder_prompt_embeddings is not None:
            #     self.encoder_prompt_embeddings['tgt'] = -self.encoder_prompt_embeddings['src']
            # if self.decoder_prompt_embeddings is not None:
            #     self.decoder_prompt_embeddings['tgt'] = -self.decoder_prompt_embeddings['src']
        elif self.prompt_domain_type == 'tgt_only':
            if self.encoder_prompt_embeddings is not None:
                self.encoder_prompt_embeddings['tgt'] = self.encoder_prompt_embeddings['src']
                self.encoder_prompt_embeddings['src'] = None
            if self.decoder_prompt_embeddings is not None:
                self.decoder_prompt_embeddings['tgt'] = self.decoder_prompt_embeddings['src']
                self.decoder_prompt_embeddings['src'] = None
        else:
            raise ValueError('Wrong setting of prompt')
            
        if self.prompt_project:
            self.prompt_proj = nn.Linear(hidden_dim, hidden_dim)
            nn.init.kaiming_normal_(self.prompt_proj.weight, a=0, mode='fan_out')
        else:
            self.prompt_proj = nn.Identity()

        self.prompt_dropout = nn.Dropout(self.prompt_dropout_rate)

    def forward(self, samples: NestedTensor, *args, **kwargs):
        """ The forward expects a NestedTensor, which consists of:
               - samples.tensor: batched images, of shape [batch_size x 3 x H x W]
               - samples.mask: a binary mask of shape [batch_size x H x W], containing 1 on padded pixels

            It returns a dict with the following elements:
               - "pred_logits": the classification logits (including no-object) for all queries.
                                Shape= [batch_size x num_queries x (num_classes + 1)]
               - "pred_boxes": The normalized boxes coordinates for all queries, represented as
                               (center_x, center_y, height, width). These values are normalized in [0, 1],
                               relative to the size of each individual image (disregarding possible padding).
                               See PostProcess for information on how to retrieve the unnormalized bounding box.
               - "aux_outputs": Optional, only returned when auxilary losses are activated. It is a list of
                                dictionnaries containing the two above keys for each decoder layer.
        """
        if not isinstance(samples, NestedTensor):
            samples = nested_tensor_from_tensor_list(samples)
        features, pos = self.backbone(samples)

        srcs = []
        masks = []
        for l, feat in enumerate(features):
            src, mask = feat.decompose()
            srcs.append(self.input_proj[l](src))
            masks.append(mask)
            assert mask is not None
        if self.num_feature_levels > len(srcs):
            _len_srcs = len(srcs)
            for l in range(_len_srcs, self.num_feature_levels):
                if l == _len_srcs:
                    src = self.input_proj[l](features[-1].tensors)
                else:
                    src = self.input_proj[l](srcs[-1])
                m = samples.mask
                mask = F.interpolate(m[None].float(), size=src.shape[-2:]).to(torch.bool)[0]
                pos_l = self.backbone[1](NestedTensor(src, mask)).to(src.dtype)
                srcs.append(src)
                masks.append(mask)
                pos.append(pos_l)

        if self.prompt_domain_type == 'inverse':
            self.encoder_prompt_embeddings['tgt'] = []
            if isinstance(self.encoder_prompt_embeddings['src'][0], nn.Parameter):
                for feat_lvl_prompt_embeddings in self.encoder_prompt_embeddings['src']:
                    self.encoder_prompt_embeddings['tgt'].append(- feat_lvl_prompt_embeddings)
            elif isinstance(self.encoder_prompt_embeddings['src'][0], nn.ParameterList):
                for layer_prompt_embeddings in self.encoder_prompt_embeddings['src']:
                    self.encoder_prompt_embeddings['tgt'].append([])
                    for feat_lvl_prompt_embeddings in layer_prompt_embeddings:
                        self.encoder_prompt_embeddings['tgt'][-1].append(- feat_lvl_prompt_embeddings)

        if 'data_domain_type' not in kwargs:
            # default
            kwargs['data_domain_type'] = 'src+tgt'
        
        if kwargs['data_domain_type'] == 'src_only':
            src_encoder_prompt_embeddings = self.encoder_prompt_embeddings['src']
            tgt_encoder_prompt_embeddings = None
        elif kwargs['data_domain_type'] == 'tgt_only':
            src_encoder_prompt_embeddings = None
            tgt_encoder_prompt_embeddings = self.encoder_prompt_embeddings['tgt']
        elif kwargs['data_domain_type'] == 'src+tgt':
            src_encoder_prompt_embeddings = self.encoder_prompt_embeddings['src']
            tgt_encoder_prompt_embeddings = self.encoder_prompt_embeddings['tgt']

        # xxx_encoder_prompt_embeddings:
        #   shallow: [num_feature_levels](h, w, hidden_dim)
        #   deep shared: [num_feature_levels](h, w, hidden_dim)
        #   deep: [num_layers][num_feature_levels](h, w, hidden_dim)

        # if kwargs['data_domain_type'] == 'src_only':
        #     import pdb; pdb.set_trace()  # check `self.encoder_prompt_embeddings` & `self.decoder_prompt_embeddings`
        
        query_embeds = None
        if not self.two_stage:
            query_embeds = self.query_embed.weight

        memory, hs, init_reference, inter_references, enc_outputs_class, enc_outputs_coord_unact, da_output = self.transformer(
            srcs, masks, pos, query_embeds,
            src_encoder_prompt_embeddings, tgt_encoder_prompt_embeddings
        )

        outputs_classes = []
        outputs_coords = []
        for lvl in range(hs.shape[0]):
            if lvl == 0:
                reference = init_reference
            else:
                reference = inter_references[lvl - 1]
            reference = inverse_sigmoid(reference)
            outputs_class = self.class_embed[lvl](hs[lvl])
            tmp = self.bbox_embed[lvl](hs[lvl])
            if reference.shape[-1] == 4:
                tmp += reference
            else:
                assert reference.shape[-1] == 2
                tmp[..., :2] += reference
            outputs_coord = tmp.sigmoid()
            outputs_classes.append(outputs_class)
            outputs_coords.append(outputs_coord)
        outputs_class = torch.stack(outputs_classes)
        outputs_coord = torch.stack(outputs_coords)

        if self.training and self.uda:
            B = outputs_class.shape[1]
            outputs_class = outputs_class[:, :B//2]
            outputs_coord = outputs_coord[:, :B//2]
            if self.two_stage:
                enc_outputs_class = enc_outputs_class[:B//2]
                enc_outputs_coord_unact = enc_outputs_coord_unact[:B//2]
            if self.backbone_align:
                da_output['backbone'] = torch.cat([self.backbone_D(self.grl(src.flatten(2).transpose(1, 2))) for src in srcs], dim=1)
            if self.space_align:
                da_output['space_query'] = self.space_D(da_output['space_query'])
            if self.channel_align:
                da_output['channel_query'] = self.channel_D(da_output['channel_query'])
            if self.instance_align:
                da_output['instance_query'] = self.instance_D(da_output['instance_query'])

        out = {'pred_logits': outputs_class[-1], 'pred_boxes': outputs_coord[-1]}
        if self.aux_loss:
            out['aux_outputs'] = self._set_aux_loss(outputs_class, outputs_coord)

        if self.two_stage:
            enc_outputs_coord = enc_outputs_coord_unact.sigmoid()
            out['enc_outputs'] = {'pred_logits': enc_outputs_class, 'pred_boxes': enc_outputs_coord}

        if self.training and self.uda:
            out['da_output'] = da_output

        return out

    @torch.jit.unused
    def _set_aux_loss(self, outputs_class, outputs_coord):
        # this is a workaround to make torchscript happy, as torchscript
        # doesn't support dictionary with non-homogeneous values, such
        # as a dict having both a Tensor and a list.
        return [{'pred_logits': a, 'pred_boxes': b}
                for a, b in zip(outputs_class[:-1], outputs_coord[:-1])]


class SetCriterion(nn.Module):
    """ This class computes the loss for DETR.
    The process happens in two steps:
        1) we compute hungarian assignment between ground truth boxes and the outputs of the model
        2) we supervise each pair of matched ground-truth / prediction (supervise class and box)
    """
    def __init__(self, num_classes, matcher, weight_dict, losses, focal_alpha=0.25, da_gamma=2):
        """ Create the criterion.
        Parameters:
            num_classes: number of object categories, omitting the special no-object category
            matcher: module able to compute a matching between targets and proposals
            weight_dict: dict containing as key the names of the losses and as values their relative weight.
            losses: list of all the losses to be applied. See get_loss for list of available losses.
            focal_alpha: alpha in Focal Loss
        """
        super().__init__()
        self.num_classes = num_classes
        self.matcher = matcher
        self.weight_dict = weight_dict
        self.losses = losses
        self.focal_alpha = focal_alpha
        self.da_gamma = da_gamma

    def loss_labels(self, outputs, targets, indices, num_boxes, log=True):
        """Classification loss (NLL)
        targets dicts must contain the key "labels" containing a tensor of dim [nb_target_boxes]
        """
        assert 'pred_logits' in outputs
        src_logits = outputs['pred_logits']

        idx = self._get_src_permutation_idx(indices)
        target_classes_o = torch.cat([t["labels"][J] for t, (_, J) in zip(targets, indices)])
        target_classes = torch.full(src_logits.shape[:2], self.num_classes,
                                    dtype=torch.int64, device=src_logits.device)
        target_classes[idx] = target_classes_o

        target_classes_onehot = torch.zeros([src_logits.shape[0], src_logits.shape[1], src_logits.shape[2] + 1],
                                            dtype=src_logits.dtype, layout=src_logits.layout, device=src_logits.device)
        target_classes_onehot.scatter_(2, target_classes.unsqueeze(-1), 1)

        target_classes_onehot = target_classes_onehot[:,:,:-1]
        loss_ce = sigmoid_focal_loss(src_logits, target_classes_onehot, num_boxes, alpha=self.focal_alpha, gamma=2) * src_logits.shape[1]
        losses = {'loss_ce': loss_ce}

        if log:
            # TODO this should probably be a separate loss, not hacked in this one here
            losses['class_error'] = 100 - accuracy(src_logits[idx], target_classes_o)[0]
        return losses

    @torch.no_grad()
    def loss_cardinality(self, outputs, targets, indices, num_boxes):
        """ Compute the cardinality error, ie the absolute error in the number of predicted non-empty boxes
        This is not really a loss, it is intended for logging purposes only. It doesn't propagate gradients
        """
        pred_logits = outputs['pred_logits']
        device = pred_logits.device
        tgt_lengths = torch.as_tensor([len(v["labels"]) for v in targets], device=device)
        # Count the number of predictions that are NOT "no-object" (which is the last class)
        card_pred = (pred_logits.argmax(-1) != pred_logits.shape[-1] - 1).sum(1)
        card_err = F.l1_loss(card_pred.float(), tgt_lengths.float())
        losses = {'cardinality_error': card_err}
        return losses

    def loss_boxes(self, outputs, targets, indices, num_boxes):
        """Compute the losses related to the bounding boxes, the L1 regression loss and the GIoU loss
           targets dicts must contain the key "boxes" containing a tensor of dim [nb_target_boxes, 4]
           The target boxes are expected in format (center_x, center_y, h, w), normalized by the image size.
        """
        assert 'pred_boxes' in outputs
        idx = self._get_src_permutation_idx(indices)
        src_boxes = outputs['pred_boxes'][idx]
        target_boxes = torch.cat([t['boxes'][i] for t, (_, i) in zip(targets, indices)], dim=0)

        loss_bbox = F.l1_loss(src_boxes, target_boxes, reduction='none')

        losses = {}
        losses['loss_bbox'] = loss_bbox.sum() / num_boxes

        loss_giou = 1 - torch.diag(box_ops.generalized_box_iou(
            box_ops.box_cxcywh_to_xyxy(src_boxes),
            box_ops.box_cxcywh_to_xyxy(target_boxes)))
        losses['loss_giou'] = loss_giou.sum() / num_boxes
        return losses

    def loss_masks(self, outputs, targets, indices, num_boxes):
        """Compute the losses related to the masks: the focal loss and the dice loss.
           targets dicts must contain the key "masks" containing a tensor of dim [nb_target_boxes, h, w]
        """
        assert "pred_masks" in outputs

        src_idx = self._get_src_permutation_idx(indices)
        tgt_idx = self._get_tgt_permutation_idx(indices)

        src_masks = outputs["pred_masks"]

        # TODO use valid to mask invalid areas due to padding in loss
        target_masks, valid = nested_tensor_from_tensor_list([t["masks"] for t in targets]).decompose()
        target_masks = target_masks.to(src_masks)

        src_masks = src_masks[src_idx]
        # upsample predictions to the target size
        src_masks = interpolate(src_masks[:, None], size=target_masks.shape[-2:],
                                mode="bilinear", align_corners=False)
        src_masks = src_masks[:, 0].flatten(1)

        target_masks = target_masks[tgt_idx].flatten(1)

        losses = {
            "loss_mask": sigmoid_focal_loss(src_masks, target_masks, num_boxes),
            "loss_dice": dice_loss(src_masks, target_masks, num_boxes),
        }
        return losses

    def loss_da(self, outputs, use_focal=False):
        B = outputs.shape[0]
        assert B % 2 == 0

        targets = torch.empty_like(outputs)
        targets[:B//2] = 0
        targets[B//2:] = 1

        loss = F.binary_cross_entropy_with_logits(outputs, targets, reduction='none')

        if use_focal:
            prob = outputs.sigmoid()
            p_t = prob * targets + (1 - prob) * (1 - targets)
            loss = loss * ((1 - p_t) ** self.da_gamma)

        return loss.mean()

    def _get_src_permutation_idx(self, indices):
        # permute predictions following indices
        batch_idx = torch.cat([torch.full_like(src, i) for i, (src, _) in enumerate(indices)])
        src_idx = torch.cat([src for (src, _) in indices])
        return batch_idx, src_idx

    def _get_tgt_permutation_idx(self, indices):
        # permute targets following indices
        batch_idx = torch.cat([torch.full_like(tgt, i) for i, (_, tgt) in enumerate(indices)])
        tgt_idx = torch.cat([tgt for (_, tgt) in indices])
        return batch_idx, tgt_idx

    def get_loss(self, loss, outputs, targets, indices, num_boxes, **kwargs):
        loss_map = {
            'labels': self.loss_labels,
            'cardinality': self.loss_cardinality,
            'boxes': self.loss_boxes,
            'masks': self.loss_masks
        }
        assert loss in loss_map, f'do you really want to compute {loss} loss?'
        return loss_map[loss](outputs, targets, indices, num_boxes, **kwargs)

    def forward(self, outputs, targets):
        """ This performs the loss computation.
        Parameters:
             outputs: dict of tensors, see the output specification of the model for the format
             targets: list of dicts, such that len(targets) == batch_size.
                      The expected keys in each dict depends on the losses applied, see each loss' doc
        """
        outputs_without_aux = {k: v for k, v in outputs.items() if k != 'aux_outputs' and k != 'enc_outputs'}

        # Retrieve the matching between the outputs of the last layer and the targets
        targets = targets[:len(outputs_without_aux['pred_logits'])]  # get source targets
        indices = self.matcher(outputs_without_aux, targets)

        # Compute the average number of target boxes accross all nodes, for normalization purposes
        num_boxes = sum(len(t["labels"]) for t in targets)
        num_boxes = torch.as_tensor([num_boxes], dtype=torch.float, device=next(iter(outputs.values())).device)
        if is_dist_avail_and_initialized():
            torch.distributed.all_reduce(num_boxes)
        num_boxes = torch.clamp(num_boxes / get_world_size(), min=1).item()

        # Compute all the requested losses
        losses = {}
        for loss in self.losses:
            kwargs = {}
            losses.update(self.get_loss(loss, outputs, targets, indices, num_boxes, **kwargs))

        # In case of auxiliary losses, we repeat this process with the output of each intermediate layer.
        if 'aux_outputs' in outputs:
            for i, aux_outputs in enumerate(outputs['aux_outputs']):
                indices = self.matcher(aux_outputs, targets)
                for loss in self.losses:
                    if loss == 'masks':
                        # Intermediate masks losses are too costly to compute, we ignore them.
                        continue
                    kwargs = {}
                    if loss == 'labels':
                        # Logging is enabled only for the last layer
                        kwargs['log'] = False
                    l_dict = self.get_loss(loss, aux_outputs, targets, indices, num_boxes, **kwargs)
                    l_dict = {k + f'_{i}': v for k, v in l_dict.items()}
                    losses.update(l_dict)

        if 'da_output' in outputs:
            for k, v in outputs['da_output'].items():
                losses[f'loss_{k}'] = self.loss_da(v, use_focal='query' in k)

        return losses


class PostProcess(nn.Module):
    """ This module converts the model's output into the format expected by the coco api"""

    @torch.no_grad()
    def forward(self, outputs, target_sizes):
        """ Perform the computation
        Parameters:
            outputs: raw outputs of the model
            target_sizes: tensor of dimension [batch_size x 2] containing the size of each images of the batch
                          For evaluation, this must be the original image size (before any data augmentation)
                          For visualization, this should be the image size after data augment, but before padding
        """
        out_logits, out_bbox = outputs['pred_logits'], outputs['pred_boxes']

        assert len(out_logits) == len(target_sizes)
        assert target_sizes.shape[1] == 2

        prob = out_logits.sigmoid()
        topk_values, topk_indexes = torch.topk(prob.view(out_logits.shape[0], -1), 100, dim=1)
        scores = topk_values
        topk_boxes = topk_indexes // out_logits.shape[2]
        labels = topk_indexes % out_logits.shape[2]
        boxes = box_ops.box_cxcywh_to_xyxy(out_bbox)
        boxes = torch.gather(boxes, 1, topk_boxes.unsqueeze(-1).repeat(1,1,4))

        # and from relative [0, 1] to absolute [0, height] coordinates
        img_h, img_w = target_sizes.unbind(1)
        scale_fct = torch.stack([img_w, img_h, img_w, img_h], dim=1)
        boxes = boxes * scale_fct[:, None, :]

        results = [{'scores': s, 'labels': l, 'boxes': b} for s, l, b in zip(scores, labels, boxes)]

        return results


class MLP(nn.Module):
    """ Very simple multi-layer perceptron (also called FFN)"""

    def __init__(self, input_dim, hidden_dim, output_dim, num_layers):
        super().__init__()
        self.num_layers = num_layers
        h = [hidden_dim] * (num_layers - 1)
        self.layers = nn.ModuleList(nn.Linear(n, k) for n, k in zip([input_dim] + h, h + [output_dim]))

    def forward(self, x):
        for i, layer in enumerate(self.layers):
            x = F.relu(layer(x)) if i < self.num_layers - 1 else layer(x)
        return x


def build(cfg):
    if not cfg.MODEL.VISUAL_PROMPT.SWITCH:
        raise ImportError('Wrong import! This module is only for visual prompt')
    if cfg.MODEL.VISUAL_PROMPT.LOCATION != 'add':
        raise ImportError('Wrong import! This module is only for that visual prompts are add')
    if cfg.MODEL.VISUAL_PROMPT.MODULES != ['encoder']:
        raise ValueError(f'Wrong key value! Only encoder can use visual prompt with location of add')
    if cfg.MODEL.VISUAL_PROMPT.DOMAIN_TYPE not in ['same', 'separate', 'inverse', 'tgt_only']:
        raise ValueError(f'Wrong key value! `DOMAIN_TYPE` should be one of "same", "separate", "inverse", or "tgt_only", but got {cfg.MODEL.VISUAL_PROMPT.DOMAIN_TYPE}')

    device = torch.device(cfg.DEVICE)

    backbone = build_backbone(cfg)

    transformer = build_deforamble_transformer(cfg)
    model = DeformableDETR(
        backbone,
        transformer,
        num_classes=cfg.DATASET.NUM_CLASSES,
        num_queries=cfg.MODEL.NUM_QUERIES,
        num_feature_levels=cfg.MODEL.NUM_FEATURE_LEVELS,
        aux_loss=cfg.LOSS.AUX_LOSS,
        with_box_refine=cfg.MODEL.WITH_BOX_REFINE,
        two_stage=cfg.MODEL.TWO_STAGE,
        backbone_align=cfg.MODEL.BACKBONE_ALIGN,
        space_align=cfg.MODEL.SPACE_ALIGN,
        channel_align=cfg.MODEL.CHANNEL_ALIGN,
        instance_align=cfg.MODEL.INSTANCE_ALIGN,
        prompt_modules=cfg.MODEL.VISUAL_PROMPT.MODULES,
        prompt_project=cfg.MODEL.VISUAL_PROMPT.PROJECT,
        deep_prompt=cfg.MODEL.VISUAL_PROMPT.DEEP,
        deep_shared_prompt=cfg.MODEL.VISUAL_PROMPT.DEEP_SHARED,
        num_prompt_tokens=cfg.MODEL.VISUAL_PROMPT.NUM_TOKENS,
        prompt_dropout_rate=cfg.MODEL.VISUAL_PROMPT.DROPOUT_RATE,
        prompt_init_a=cfg.MODEL.VISUAL_PROMPT.INIT_A,
        prompt_init_b=cfg.MODEL.VISUAL_PROMPT.INIT_B,
        prompt_domain_type=cfg.MODEL.VISUAL_PROMPT.DOMAIN_TYPE
    )
    if cfg.MODEL.MASKS:
        model = DETRsegm(model, freeze_detr=(cfg.MODEL.FROZEN_WEIGHTS is not None))
    matcher = build_matcher(cfg)
    weight_dict = {'loss_ce': cfg.LOSS.CLS_LOSS_COEF, 'loss_bbox': cfg.LOSS.BBOX_LOSS_COEF}
    weight_dict['loss_giou'] = cfg.LOSS.GIOU_LOSS_COEF
    if cfg.MODEL.MASKS:
        weight_dict["loss_mask"] = cfg.LOSS.MASK_LOSS_COEF
        weight_dict["loss_dice"] = cfg.LOSS.DICE_LOSS_COEF
    # TODO this is a hack
    if cfg.LOSS.AUX_LOSS:
        aux_weight_dict = {}
        for i in range(cfg.MODEL.DEC_LAYERS - 1):
            aux_weight_dict.update({k + f'_{i}': v for k, v in weight_dict.items()})
        aux_weight_dict.update({k + f'_enc': v for k, v in weight_dict.items()})
        weight_dict.update(aux_weight_dict)

    weight_dict['loss_backbone'] = cfg.LOSS.BACKBONE_LOSS_COEF
    weight_dict['loss_space_query'] = cfg.LOSS.SPACE_QUERY_LOSS_COEF
    weight_dict['loss_channel_query'] = cfg.LOSS.CHANNEL_QUERY_LOSS_COEF
    weight_dict['loss_instance_query'] = cfg.LOSS.INSTANCE_QUERY_LOSS_COEF

    losses = ['labels', 'boxes', 'cardinality']
    if cfg.MODEL.MASKS:
        losses += ["masks"]
    # num_classes, matcher, weight_dict, losses, focal_alpha=0.25
    criterion = SetCriterion(cfg.DATASET.NUM_CLASSES, matcher, weight_dict, losses, focal_alpha=cfg.LOSS.FOCAL_ALPHA, da_gamma=cfg.LOSS.DA_GAMMA)
    criterion.to(device)
    postprocessors = {'bbox': PostProcess()}
    if cfg.MODEL.MASKS:
        postprocessors['segm'] = PostProcessSegm()
        if cfg.DATASET.DATASET_FILE == "coco_panoptic":
            is_thing_map = {i: i <= 90 for i in range(201)}
            postprocessors["panoptic"] = PostProcessPanoptic(is_thing_map, threshold=0.85)

    return model, criterion, postprocessors