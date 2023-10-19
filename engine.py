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
Train and eval functions used in main.py
"""
import math
import os
import sys
from typing import Iterable

import torch
import torch.nn as nn
import util.misc as utils
from models.utils import DomainAttention
from datasets.coco_eval import CocoEvaluator
from datasets.panoptic_eval import PanopticEvaluator
from datasets.data_prefetcher import data_prefetcher
from util.box_ops import plot_results
from util.plot_utils import plot_bbox, plot_tgt_map
import matplotlib.pyplot as plt
from PIL import Image
import numpy as np
from pathlib import Path

def train_one_epoch(model: torch.nn.Module, criterion: torch.nn.Module,
                    data_loader: Iterable, optimizer: torch.optim.Optimizer,
                    device: torch.device, epoch: int,
                    postprocessors, cfg=None, **kwargs):
    model.train()
    if cfg.MODEL.VISUAL_PROMPT.SWITCH:
        # set model in evaluation (inference) mode
        # because `DeformableDETR` has group norm layers and
        # `DeformableTransformer` has dropout and layer norm layers
        for layers in model.module.input_proj:
            for l in layers:
                if isinstance(l, nn.GroupNorm):
                    l.eval()
        for encoder_layer in model.module.transformer.encoder.layers:
            encoder_layer.dropout1.eval()
            encoder_layer.dropout2.eval()
            encoder_layer.dropout3.eval()
            encoder_layer.norm1.eval()
            encoder_layer.norm2.eval()

            if hasattr(encoder_layer, 'space_attn'):
                for m in encoder_layer.space_attn.modules():
                    if isinstance(m, (nn.LayerNorm, nn.Dropout, nn.MultiheadAttention)):
                        m.eval()
            if hasattr(encoder_layer, 'channel_attn'):
                for m in encoder_layer.channel_attn.modules():
                    if isinstance(m, (nn.LayerNorm, nn.Dropout, nn.MultiheadAttention)):
                        m.eval()
        for decoder_layer in model.module.transformer.decoder.layers:
            decoder_layer.self_attn.eval()
            decoder_layer.dropout1.eval()
            decoder_layer.dropout2.eval()
            decoder_layer.dropout3.eval()
            decoder_layer.dropout4.eval()
            decoder_layer.norm1.eval()
            decoder_layer.norm2.eval()
            decoder_layer.norm3.eval()

            if hasattr(decoder_layer, 'instance_attn'):
                for m in decoder_layer.instance_attn.modules():
                    if isinstance(m, (nn.LayerNorm, nn.Dropout, nn.MultiheadAttention)):
                        m.eval()
            
    criterion.train()
    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('class_error', utils.SmoothedValue(window_size=1, fmt='{value:.2f}'))
    metric_logger.add_meter('grad_norm', utils.SmoothedValue(window_size=1, fmt='{value:.2f}'))
    if hasattr(cfg.MODEL, 'VISUAL_PROMPT') and cfg.MODEL.VISUAL_PROMPT.SWITCH:
        metric_logger.add_meter('lr_head', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
        metric_logger.add_meter('lr_prompt', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
        metric_logger.add_meter('min_prompt_norm', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
        metric_logger.add_meter('max_prompt_norm', utils.SmoothedValue(window_size=1, fmt='{value:.6f}')) 
        metric_logger.add_meter('prompt_grad_norm', utils.SmoothedValue(window_size=1, fmt='{value:.10f}'))
    else:
        metric_logger.add_meter('lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    header = 'Epoch: [{}]'.format(epoch)
    print_freq = 1
    
    prefetcher = data_prefetcher(data_loader, device, prefetch=True)
    samples, targets = prefetcher.next()  # samples have been transformed at this stage

    data_loader_len = len(data_loader)
    data_domain_type = getattr(data_loader.dataset, 'data_domain_type', 'src+tgt')
    
    # for samples, targets in metric_logger.log_every(data_loader, print_freq, header):
    ### value on the left is current and value on the right is smoothed value
    thresh_record = []
    missing_source = 0
    for iter_i in metric_logger.log_every(range(data_loader_len), print_freq, header):
        # for debug
        # if iter_i == 10:
        #     break

        # BUG counting number of missing source targets
        for t in targets:
            if t['labels'].nelement() == 0:
                missing_source += 1
                continue

        # DEBUG for nan grad
        # with torch.autograd.detect_anomaly():
        if cfg.MODEL.VISUAL_PROMPT.SWITCH and cfg.MODEL.VISUAL_PROMPT.DOMAIN_TYPE == 'tgt_only':
            assert samples.tensors.shape[0] % 2 == 0, samples.tensors.shape
            bs = samples.tensors.shape[0]
            src_samples = utils.nested_tensor_from_tensor_list(samples.tensors[:bs//2])
            src_targets = targets[:bs//2]
            tgt_samples = utils.nested_tensor_from_tensor_list(samples.tensors[bs//2:])
            tgt_targets = targets[bs//2:]

            src_outputs = model(src_samples, data_domain_type='src_only')
            tgt_outputs = model(tgt_samples, data_domain_type='tgt_only')

            if cfg.MODEL.VISUAL_PROMPT.DROP_PROMPT_OUTPUTS:
                outputs = {}

                tgt_outputs['pred_logits'] = tgt_outputs['pred_logits'][:, :cfg.MODEL.NUM_QUERIES, :]
                outputs['pred_logits'] = torch.cat([
                    src_outputs['pred_logits'],
                    tgt_outputs['pred_logits']
                    ], dim=0)

                tgt_outputs['pred_boxes'] = tgt_outputs['pred_boxes'][:, :cfg.MODEL.NUM_QUERIES, :]
                outputs['pred_boxes'] = torch.cat([
                    src_outputs['pred_boxes'],
                    tgt_outputs['pred_boxes']
                    ], dim=0)

                outputs['aux_outputs'] = src_outputs['aux_outputs']
                # outputs['aux_outputs'][0]['pred_logits']: (1, 300, 9)
                # outputs['aux_outputs'][0]['pred_boxes']: (1, 300, 9)

                outputs['da_output'] = {}
                for x_query in tgt_outputs['da_output'].keys():
                    outputs['da_output'][x_query] = torch.cat([
                        src_outputs['da_output'][x_query],
                        tgt_outputs['da_output'][x_query]
                    ])                
            else:
                raise NotImplementedError()

            loss_dict = criterion(outputs, targets)
            weight_dict = criterion.weight_dict
        else:
            outputs = model(samples, data_domain_type=data_domain_type)
            # outputs['aux_outputs'][0]['pred_logits']: (1, #queries, 9)
            # outputs['aux_outputs'][0]['pred_boxes']: (1, #queries, 9)

            if cfg.MODEL.VISUAL_PROMPT.DROP_PROMPT_OUTPUTS:
                outputs['pred_logits'] = outputs['pred_logits'][:, :cfg.MODEL.NUM_QUERIES, :]
                outputs['pred_boxes'] = outputs['pred_boxes'][:, :cfg.MODEL.NUM_QUERIES, :]
                for i in range(len(outputs['aux_outputs'])):
                    outputs['aux_outputs'][i]['pred_logits'] = outputs['aux_outputs'][i]['pred_logits'][:, :cfg.MODEL.NUM_QUERIES, :]
                    outputs['aux_outputs'][i]['pred_boxes'] = outputs['aux_outputs'][i]['pred_boxes'][:, :cfg.MODEL.NUM_QUERIES, :]

            loss_dict = criterion(outputs, targets)
            weight_dict = criterion.weight_dict

        keys = [k for k in loss_dict.keys() if 'query_idx_class_label_pairs' in k]
        [loss_dict.pop(k) for k in keys]

        # the loss used for optimization
        losses = sum(loss_dict[k] * weight_dict[k] for k in loss_dict.keys() if k in weight_dict)

        # reduce losses over all GPUs for logging purposes (loss_dict_reduced is scaled)
        loss_dict_reduced = utils.reduce_dict(loss_dict)

        # store unscaled losses
        loss_dict_reduced_unscaled = {f'{k}_unscaled': v
                                    for k, v in loss_dict_reduced.items()}

        # store scaled losses
        loss_dict_reduced_scaled = {k: v * weight_dict[k]
                                    for k, v in loss_dict_reduced.items() if k in weight_dict}

        # get total loss
        losses_reduced_scaled = sum(loss_dict_reduced_scaled.values())
        loss_value = losses_reduced_scaled.item()

        # check if total loss is inf
        if not math.isfinite(loss_value):
            print("Loss is {}, stopping training".format(loss_value))
            print(loss_dict_reduced)
            sys.exit(1)

        optimizer.zero_grad()
        losses.backward()
        if cfg.TRAIN.CLIP_MAX_NORM > 0:
            # compute gradient norm
            grad_total_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), cfg.TRAIN.CLIP_MAX_NORM)
        else:
            grad_total_norm = utils.get_total_grad_norm(model.parameters(), cfg.TRAIN.CLIP_MAX_NORM)
        optimizer.step()
        
        metric_logger.update(loss=loss_value, **loss_dict_reduced_scaled, **loss_dict_reduced_unscaled)
        metric_logger.update(class_error=loss_dict_reduced['class_error'])
        metric_logger.update(grad_norm=grad_total_norm)

        # in visual prompt tuning
        if hasattr(cfg.MODEL, 'VISUAL_PROMPT') and cfg.MODEL.VISUAL_PROMPT.SWITCH:
            prompt_parameters = [p for n, p in model.named_parameters() if 'prompt_embeddings' in n]
            prompt_norms = [torch.norm(p, dim=-1) for p in prompt_parameters]
            min_prompt_norm = min([torch.min(n).item() for n in prompt_norms])
            max_prompt_norm = max([torch.max(n).item() for n in prompt_norms])

            metric_logger.update(lr_head=optimizer.param_groups[0]['lr'])
            metric_logger.update(lr_prompt=optimizer.param_groups[1]['lr'])
            metric_logger.update(min_prompt_norm=min_prompt_norm)
            metric_logger.update(max_prompt_norm=max_prompt_norm)

            if optimizer.param_groups[-1]['params']:
                prompt_grad_norm = utils.get_total_grad_norm(optimizer.param_groups[-1]['params'])
                metric_logger.update(prompt_grad_norm=prompt_grad_norm)
            else:
                metric_logger.update(prompt_grad_norm=0)
        else:
            metric_logger.update(lr=optimizer.param_groups[0]['lr'])

        # ↓↓↓ plot pseudo boxes ↓↓↓
        # if cfg.PLOT.PLOT_BBOX and 'tgt_pred_logits' in outputs:
        #     B = len(targets)
        #     tgt_tensors = samples.tensors[B//2:]
        #     tgt_targets = targets[B//2:]
        #     tgt_outputs = {
        #         'pred_logits': outputs['tgt_pred_logits'],
        #         'pred_boxes': outputs['tgt_pred_boxes']
        #     }
        #     orig_tgt_target_sizes = torch.stack([t["size"] for t in tgt_targets], dim=0)
        #     results = postprocessors['bbox'](tgt_outputs, orig_tgt_target_sizes)
        #     res = {target['image_id'].item(): output for target, output in zip(tgt_targets, results)}
        #     img_tensors = {target['image_id'].item(): output for target, output in zip(tgt_targets, tgt_tensors)}
            
        #     plot_bbox(
        #         img_tensors=img_tensors,
        #         res=res,
        #         coco=data_loader.dataset.target.coco,
        #         box_save_dir=Path(cfg.OUTPUT_DIR) / 'plot_bbox',
        #         score_threshold=cfg.PLOT.SCORE_THRESHOLD,
        #         img_ids=cfg.PLOT.IMG_IDS,
        #         prefix=kwargs['prefix']
        #     )
        # ↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑

        # ↓↓↓ plot proposal score map ↓↓↓
        # if cfg.PLOT.PLOT_MAP and 'tgt_map_out' in outputs:
        #     B = len(targets)
        #     tgt_targets = targets[B//2:]
        #     tgt_tensors = samples.tensors[B//2:]
        #     tgt_tensors = {target['image_id'].item(): output for target, output in zip(tgt_targets, tgt_tensors)}

        #     plot_tgt_map(
        #         tgt_tensors=tgt_tensors,
        #         tgt_res=outputs['tgt_map_out'],
        #         coco=data_loader.dataset.target.coco,
        #         map_save_dir=Path(cfg.OUTPUT_DIR) / 'plot_map',
        #         img_ids=cfg.PLOT.IMG_IDS,
        #         prefix=kwargs['prefix']
        #     )
        # ↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑

        samples, targets = prefetcher.next()
    
    print('missing_source:', missing_source)
    
    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    return {k: meter.global_avg for k, meter in metric_logger.meters.items()}, outputs


@torch.no_grad()
def evaluate(model, criterion, postprocessors, data_loader, base_ds, device, cfg, **kwargs):
    """
    data_loader.dataset: `CocoDetection`, not `DADataset`
    base_ds: `pycocotools.coco.COCO`
    """

    model.eval()
    criterion.eval()

    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('class_error', utils.SmoothedValue(window_size=1, fmt='{value:.2f}'))
    header = 'Test:'

    iou_types = tuple(k for k in ('segm', 'bbox') if k in postprocessors.keys())
    coco_evaluator = CocoEvaluator(base_ds, iou_types)
    coco_evaluators_per_class = {}
    for cat_id in base_ds.getCatIds():
        if 'category_ids' in kwargs:
            if cat_id not in cfg.DATASET.CATEGORY_IDS:
                continue
        evaluator = CocoEvaluator(base_ds, iou_types)
        for iou_type in iou_types:
            evaluator.coco_eval[iou_type].params.catIds = [cat_id]
        coco_evaluators_per_class[cat_id] = evaluator
    # coco_evaluator.coco_eval[iou_types[0]].params.iouThrs = [0, 0.1, 0.5, 0.75]

    panoptic_evaluator = None
    if 'panoptic' in postprocessors.keys():
        panoptic_evaluator = PanopticEvaluator(
            data_loader.dataset.ann_file,
            data_loader.dataset.ann_folder,
            output_dir=os.path.join(cfg.OUTPUT_DIR, "panoptic_eval"),
        )

    data_domain_type = getattr(data_loader.dataset, 'data_domain_type', 'src+tgt')

    i = 0
    all_query_idx_class_label_pairs = []
    for samples, targets in metric_logger.log_every(data_loader, 10, header):
        samples = samples.to(device)
        targets = [{k: v.to(device) for k, v in t.items()} for t in targets]
        
        outputs = model(samples, data_domain_type=data_domain_type)
        loss_dict = criterion(outputs, targets)
        weight_dict = criterion.weight_dict

        all_query_idx_class_label_pairs.extend(loss_dict.pop('query_idx_class_label_pairs'))
        keys = [k for k in loss_dict.keys() if 'query_idx_class_label_pairs_' in k]
        for k in keys:
            loss_dict.pop(k)

        # reduce losses over all GPUs for logging purposes
        loss_dict_reduced = utils.reduce_dict(loss_dict)
        loss_dict_reduced_scaled = {k: v * weight_dict[k]
                                    for k, v in loss_dict_reduced.items() if k in weight_dict}
        loss_dict_reduced_unscaled = {f'{k}_unscaled': v
                                      for k, v in loss_dict_reduced.items()}
        metric_logger.update(loss=sum(loss_dict_reduced_scaled.values()),
                             **loss_dict_reduced_scaled,
                             **loss_dict_reduced_unscaled)
        metric_logger.update(class_error=loss_dict_reduced['class_error'])

        orig_target_sizes = torch.stack([t["orig_size"] for t in targets], dim=0)

        # outputs: dict storing 300 predictions
        # list storing scores, labels, and boxes (100)
        # postprocess into format accepted by coco api
        results = postprocessors['bbox'](outputs, orig_target_sizes) # at test time samples are not transformed thus use orig_target_sizes

        if 'segm' in postprocessors.keys():
            target_sizes = torch.stack([t["size"] for t in targets], dim=0)
            results = postprocessors['segm'](results, outputs, orig_target_sizes, target_sizes)
        
        # dict storing scores, labels and boxes (100 predictions) from bs target images
        # keys store key ids 
        # scores are ranked
        res = {target['image_id'].item(): output for target, output in zip(targets, results)}

        # ↓↓↓ plot pseudo boxes ↓↓↓
        if cfg.PLOT.PLOT_BBOX:
            tgt_tensors = samples.tensors
            img_tensors = {target['image_id'].item(): output for target, output in zip(targets, tgt_tensors)}

            outputs_without_prompts = {
                'pred_logits': outputs['pred_logits'][:, :cfg.MODEL.NUM_QUERIES, :],
                'pred_boxes': outputs['pred_boxes'][:, :cfg.MODEL.NUM_QUERIES, :]
            }
            print(f"find {outputs_without_prompts['pred_logits'].shape[1]} object queries")

            # images are all resized to 800, so we can't use `orig_size` to rescale bboxes
            # use `size` instead
            target_sizes = torch.stack([t["size"] for t in targets], dim=0)
            _results = postprocessors['bbox'](outputs_without_prompts, target_sizes)  # [{'scores': s, 'labels': l, 'boxes': b}, ...]
            _res = {target['image_id'].item(): output for target, output in zip(targets, _results)}
            plot_bbox(
                img_tensors=img_tensors,
                res=_res,
                coco=data_loader.dataset.coco,
                save_dir=Path(cfg.OUTPUT_DIR) / 'plot_bbox',
                score_threshold=cfg.PLOT.SCORE_THRESHOLD,
                img_ids=cfg.PLOT.IMG_IDS,
                prefix=kwargs['prefix']
            )
        # ↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑

        # ↓↓↓ plot pseudo boxes predicted by prompts ↓↓↓
        if cfg.PLOT.PLOT_PROMPT_BBOX:
            tgt_tensors = samples.tensors
            img_tensors = {target['image_id'].item(): output for target, output in zip(targets, tgt_tensors)}

            outputs_with_prompts = {
                'pred_logits': outputs['pred_logits'][:, cfg.MODEL.NUM_QUERIES:, :],
                'pred_boxes': outputs['pred_boxes'][:, cfg.MODEL.NUM_QUERIES:, :]
            }
            print(f"find {outputs_without_prompts['pred_logits'].shape[1]} prompts")

            # images are all resized to 800, so we can't use `orig_size` to rescale bboxes
            # use `size` instead
            target_sizes = torch.stack([t["size"] for t in targets], dim=0)
            _results = postprocessors['bbox'](outputs_with_prompts, target_sizes)  # [{'scores': s, 'labels': l, 'boxes': b}, ...]
            _res = {target['image_id'].item(): output for target, output in zip(targets, _results)}
            plot_bbox(
                img_tensors=img_tensors,
                res=_res,
                coco=data_loader.dataset.coco,
                save_dir=Path(cfg.OUTPUT_DIR) / 'plot_prompt_bbox',
                score_threshold=cfg.PLOT.SCORE_THRESHOLD,
                img_ids=cfg.PLOT.IMG_IDS,
                prefix=kwargs['prefix']
            )
        # ↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑

        # ↓↓↓ plot proposal score map ↓↓↓
        # if cfg.PLOT.PLOT_MAP:
        #     tgt_tensors = samples.tensors
        #     tgt_tensors = {target['image_id'].item(): output for target, output in zip(targets, tgt_tensors)}
            
        #     plot_tgt_map(
        #         tgt_tensors=tgt_tensors,
        #         tgt_res=res,
        #         coco=data_loader.dataset.coco,
        #         map_save_dir=Path(cfg.OUTPUT_DIR) / 'plot_map',
        #         img_ids=cfg.PLOT.IMG_IDS,
        #         prefix=kwargs['prefix']
        #     )
        # ↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑

        if coco_evaluator is not None:
            coco_evaluator.update(res)
        if coco_evaluators_per_class is not None:
            for evaluator in coco_evaluators_per_class.values():
                evaluator.update(res)

        if panoptic_evaluator is not None:
            res_pano = postprocessors["panoptic"](outputs, target_sizes, orig_target_sizes)
            for i, target in enumerate(targets):
                image_id = target["image_id"].item()
                file_name = f"{image_id:012d}.png"
                res_pano[i]["image_id"] = image_id
                res_pano[i]["file_name"] = file_name

            panoptic_evaluator.update(res_pano)

        # i += 1
        # if i == 5:
        #     break

    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    if coco_evaluator is not None:
        coco_evaluator.synchronize_between_processes()
    if coco_evaluators_per_class is not None:
        for evaluator in coco_evaluators_per_class.values():
            evaluator.synchronize_between_processes()
    if panoptic_evaluator is not None:
        panoptic_evaluator.synchronize_between_processes()

    # accumulate predictions from all images
    if coco_evaluator is not None:
        print('=== Overall mAP ===')
        coco_evaluator.accumulate()
        coco_evaluator.summarize()
    if coco_evaluators_per_class is not None:
        for cat_id, evaluator in coco_evaluators_per_class.items():
            cat_name = base_ds.cats[cat_id]['name']
            print(f'=== Class mAP ({cat_id}, {cat_name}) ===')
            evaluator.accumulate()
            evaluator.summarize()

    panoptic_res = None
    if panoptic_evaluator is not None:
        panoptic_res = panoptic_evaluator.summarize()
    stats = {k: meter.global_avg for k, meter in metric_logger.meters.items()}
    if coco_evaluator is not None:
        if 'bbox' in postprocessors.keys():
            if 'category_ids' in kwargs:
                ratio = (cfg.DATASET.NUM_CLASSES - 1) / len(cfg.DATASET.CATEGORY_IDS)
                stats['coco_eval_bbox'] = [v * ratio for v in coco_evaluator.coco_eval['bbox'].stats.tolist()]
            for cat_id, evaluator in coco_evaluators_per_class.items():
                cat_name = base_ds.cats[cat_id]['name']
                k = f'coco_eval_bbox_cat-id={cat_id}_cat-name={cat_name}'
                stats[k] = evaluator.coco_eval['bbox'].stats.tolist()
        if 'segm' in postprocessors.keys():
            stats['coco_eval_masks'] = coco_evaluator.coco_eval['segm'].stats.tolist()
    if panoptic_res is not None:
        stats['PQ_all'] = panoptic_res["All"]
        stats['PQ_th'] = panoptic_res["Things"]
        stats['PQ_st'] = panoptic_res["Stuff"]

    query_idx_to_class_labels = {}
    for query_indices, cat_ids in all_query_idx_class_label_pairs:
        for query_idx, cat_id in zip(query_indices, cat_ids):
            query_idx, cat_id = query_idx.item(), cat_id.item()
            if query_idx not in query_idx_to_class_labels:
                query_idx_to_class_labels[query_idx] = [cat_id]
            else:
                query_idx_to_class_labels[query_idx].append(cat_id)

    return stats, coco_evaluator, query_idx_to_class_labels
