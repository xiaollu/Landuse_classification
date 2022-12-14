import logging
import time
import os

import numpy as np
import torch
import torch.nn as nn
from numpy import linalg as LA

from lib.core.inference import get_final_preds
from lib.utils.vis import vis_seg_mask
from lib.utils.evaluation import MetricLogger
# from lib.utils.evaluation import createConfusionMatrix

logger = logging.getLogger(__name__)

def train(train_loader, train_dataset, model, criterion, optimizer, epoch, output_dir,
          writer_dict, args):
    '''Train one epoch
    
    Args:
        train_loader (torch.utils.data.DataLoader): dataloader for training set.
        model (torch.nn.Module): image segmentation module.
        criterion (torch.nn.Module): loss function for image segmentation.
        optimizer (torch.optim.Optimizer): optimizer for model parameters.
        epoch (int): current training epoch.
        output_dir (str): directory to save logs.
        writer_dict (dict): dictionary containing tensorboard related objects.
        args: arguments from the main script.
    '''
    
    data_time = AverageMeter()
    losses = AverageMeter()
    comlosses = AverageMeter()
    batch_time = AverageMeter()
    
    # switch to train mode
    model.train()
    device = torch.device("cuda")
    end = time.time()
    
    for i, (image, dem, mask) in enumerate(train_loader):
        # measure the data loading time
        data_time.update(time.time() - end)
        
        # compute output
        image = image.to(device)
        dem = dem.to(device)
        input = torch.cat((image, dem), dim=1) #[B, 4, 400, 400]
        output = model(input) #[B,16,400,400]
        
        # compute loss
        mask = mask.to(device)#[B,16,200,200]
        if args.experts == 3:
            [many_loss, medium_loss, few_loss], comloss = criterion(output, mask)
            loss = many_loss + medium_loss + few_loss           
            # compute gradient and update
            optimizer.zero_grad()
            # if train seperately:
            if args.TRAIN_SEP:
                many_loss.backward(retain_graph=True)
                for name, param in model.named_parameters():
                    if not ('medium' in name or 'few' in name):
                        param.requires_grad = False
                
                (medium_loss+few_loss).backward()
                for name, param in model.named_parameters():
                    param.requires_grad = True
            else:
                loss.backward()
            optimizer.step()
        
        if args.experts == 2:
            [many_loss, few_loss], comloss = criterion(output, mask)
            loss = many_loss + few_loss 
                    # compute gradient and update
            optimizer.zero_grad()
            # if train seperately:
            if args.TRAIN_SEP:
                many_loss.backward(retain_graph=True)
                for name, param in model.named_parameters():
                    if not ('few' in name):
                        param.requires_grad = False
                
                (few_loss).backward()
                for name, param in model.named_parameters():
                    param.requires_grad = True
            else:
                loss.backward()
            optimizer.step()
        
        # record loss
        losses.update(loss.item(), input.size(0))
        comlosses.update(comloss.item(), input.size(0))
        
        batch_time.update(time.time() - end)
        end = time.time()
        
        if i % args.frequent == 0:
            msg = 'Epoch: [{0}][{1}/{2}]\t' \
                  'Time {batch_time.val:.3f}s ({batch_time.avg:.3f}s)\t' \
                  'Speed {speed:.1f} samples/s\t' \
                  'Data {data_time.val:.3f}s ({data_time.avg:.3f}s)\t' \
                  'Loss {loss.val:.5f} ({loss.avg:.5f})\t'\
                  'ComLoss {comloss.val:.5f} ({comloss.avg:.5f})\t'.format(
                      epoch, i, len(train_loader), batch_time=batch_time,
                      speed=input.size(0)/batch_time.val,
                      data_time=data_time, loss=losses, comloss=comlosses)
            logger.info(msg)
    
    lr = optimizer.param_groups[0]['lr']
    if writer_dict:
        writer = writer_dict['logger']
        global_steps = writer_dict['train_global_steps']

        writer.add_scalar('train_loss', losses.avg, global_steps)
        writer.add_scalar('com_loss', comlosses.avg, global_steps)
        writer.add_scalar('learning_rate', lr, global_steps)
        writer_dict['train_global_steps'] = global_steps + 1
        
               
def validate(val_loader, val_dataset, model, criterion, ls_index, output_dir,
             writer_dict, args): 
    '''Validate the model
    Returns:
        perf_indicator (float): performance indicator. In the case of image segmentation, we return
                                mean IoU over all validation images.
    '''
    batch_time = AverageMeter()
    losses = AverageMeter()
    comlosses = AverageMeter()
    
    # switch to evaluate mode
    model.eval()
    
    metrics = MetricLogger(model.num_classes)
    metrics_exp1 = MetricLogger(model.num_classes)
    metrics_exp2 = MetricLogger(model.num_classes)
    device = torch.device("cuda")
        
    from collections import OrderedDict
    new_dict = OrderedDict()
    for k, v in model.named_parameters():
        if k.startswith("SegHead"):
            new_dict[k] = v
    
    if args.experts == 3:
        [many_index, medium_index, few_index] = ls_index
        weight_many = new_dict['SegHead_many.weight'].detach().cpu().numpy()
        weight_medium = new_dict['SegHead_medium.weight'].detach().cpu().numpy()
        weight_few = new_dict['SegHead_few.weight'].detach().cpu().numpy()

        weight_norm_many = LA.norm(weight_many, axis=1)
        weight_norm_medium = LA.norm(weight_medium, axis=1)
        weight_norm_few = LA.norm(weight_few, axis=1)

        m_scale = (np.mean(weight_norm_many) / np.mean(weight_norm_medium[medium_index+few_index, :]))
        f_scale = (np.mean(weight_norm_many) / np.mean(weight_norm_few[few_index,:]))

    if args.experts == 2:
        [many_index, few_index] = ls_index   
        weight_many = new_dict['SegHead_many.weight'].detach().cpu().numpy()
        weight_few = new_dict['SegHead_few.weight'].detach().cpu().numpy()

        weight_norm_many = LA.norm(weight_many, axis=1)
        weight_norm_few = LA.norm(weight_few, axis=1)
    
        f_scale = (np.mean(weight_norm_many) / np.mean(weight_norm_few[few_index,:]))
    
    with torch.no_grad():
        end = time.time()
        for i, (image, dem, mask) in enumerate(val_loader):
            # compute output
            image = image.to(device)
            dem = dem.to(device)
            input = torch.cat((image, dem), dim=1) #[B, 4, 200, 200]
            output = model(input) #[B, 10, 200, 200]
            num_inputs = input.size(0)
            # compute loss
            mask = mask.to(device) #[B, 10, 200, 200]
            
            if args.experts == 3:
                [many_loss, medium_loss, few_loss], comloss = criterion(output, mask)
                loss = many_loss + medium_loss + few_loss
                
                [many_output, medium_output, few_output] = output
                medium_output[:,many_index] = 0
                few_output[:,many_index + medium_index] = 0
                final_output = many_output + medium_output * m_scale + few_output * f_scale
                final_output[:,medium_index] /= 2
                final_output[:,few_index] /= 3
                
            if args.experts == 2:
                [many_loss, few_loss], comloss = criterion(output, mask)
                loss = many_loss + few_loss
                
                [many_output, few_output] = output
                few_output[:,many_index] = 0
                final_output = many_output + few_output * f_scale
                final_output[:,few_index] /= 2
                
            # measure accuracy and record loss
            losses.update(loss.item(), num_inputs)
            comlosses.update(comloss.item(), num_inputs)
            
            preds = get_final_preds(final_output.detach().cpu().numpy())
            preds_many = get_final_preds(many_output.detach().cpu().numpy())
            preds_few = get_final_preds(few_output.detach().cpu().numpy())
            
            gt = mask.squeeze().detach().cpu().numpy()
            
            metrics.update(gt, preds)
            metrics_exp1.update(gt, preds_many)
            metrics_exp2.update(gt, preds_few)
            
            # measure elapsed time
            batch_time.update(time.time() - end)
            end = time.time()
            
            if i % args.frequent == 0:
                msg = 'Validate: [{0}/{1}]\t' \
                      'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t' \
                      'Loss {loss.val:.4f} ({loss.avg:.4f})\t' \
                      'ComLoss {comloss.val:.4f} ({comloss.avg:.4f})'.format(
                          i, len(val_loader), batch_time=batch_time,
                          loss=losses, comloss=comlosses)

                logger.info(msg)
                           
        mean_acc, mean_iou, acc_cls, overall_acc = metrics.get_scores()
        logger.info('Mean IoU score: {:.3f}'.format(mean_iou))
        
        acc_ACE_many, acc_ACE_medium, acc_ACE_few = metrics.get_acc_cat()
        acc_exp1_many, acc_exp1_medium, acc_exp1_few = metrics_exp1.get_acc_cat()
        acc_exp2_many, acc_exp2_medium, acc_exp2_few = metrics_exp2.get_acc_cat()
        
        perf_indicator = mean_iou
        
        metrics.reset()
        metrics_exp1.reset()
        metrics_exp2.reset()
        
        if writer_dict:
            writer = writer_dict['logger']
            global_steps = writer_dict['valid_global_steps']
            writer.add_scalars('accuracy_many', {'expert_1':acc_exp1_many,
                                                'expert_2':acc_exp2_many,
                                                'ACE':acc_ACE_many}, global_steps)
            writer.add_scalars('accuracy_medium', {'expert_1':acc_exp1_medium,
                                                'expert_2':acc_exp2_medium,
                                                'ACE':acc_ACE_medium}, global_steps) 
            writer.add_scalars('accuracy_few', {'expert_1':acc_exp1_few,
                                                'expert_2':acc_exp2_few,
                                                'ACE':acc_ACE_few}, global_steps)            
            writer.add_scalar('val_com_loss', comlosses.avg, global_steps)
            writer.add_scalar('valid_loss', losses.avg, global_steps)
            writer.add_scalar('valid_iou_score', mean_iou, global_steps)
            writer_dict['valid_global_steps'] = global_steps + 1
    
    return losses.avg, perf_indicator
            
def test(test_loader, test_dataset, model, ls_index, output_dir,
             writer_dict, args): 
    '''Test the model
    Returns:
        perf_indicator (float): performance indicator. In the case of image segmentation, we return
                                mean IoU over all validation images.
    '''
    batch_time = AverageMeter()
    
    # switch to evaluate mode
    model.eval()
    metrics = MetricLogger(model.num_classes)
    metrics_exp1 = MetricLogger(model.num_classes)
    metrics_exp2 = MetricLogger(model.num_classes)
    device = torch.device("cuda")
    
    from collections import OrderedDict
    new_dict = OrderedDict()
    for k, v in model.named_parameters():
        if k.startswith("SegHead"):
            new_dict[k] = v
    
    if args.experts == 3:
        [many_index, medium_index, few_index] = ls_index
        weight_many = new_dict['SegHead_many.weight'].detach().cpu().numpy()
        weight_medium = new_dict['SegHead_medium.weight'].detach().cpu().numpy()
        weight_few = new_dict['SegHead_few.weight'].detach().cpu().numpy()

        weight_norm_many = LA.norm(weight_many, axis=1)
        weight_norm_medium = LA.norm(weight_medium, axis=1)
        weight_norm_few = LA.norm(weight_few, axis=1)
    
        f_scale = (np.mean(weight_norm_many) / np.mean(weight_norm_few[few_index,:]))
        m_scale = (np.mean(weight_norm_many) / np.mean(weight_norm_medium[medium_index+few_index, :]))
        
    if args.experts == 2:
        [many_index, few_index] = ls_index   
        weight_many = new_dict['SegHead_many.weight'].detach().cpu().numpy()
        weight_few = new_dict['SegHead_few.weight'].detach().cpu().numpy()

        weight_norm_many = LA.norm(weight_many, axis=1)
        weight_norm_few = LA.norm(weight_few, axis=1)
    
        f_scale = (np.mean(weight_norm_many) / np.mean(weight_norm_few[few_index,:]))
    
    with torch.no_grad():
        end = time.time()
        for i, (image, dem, mask) in enumerate(test_loader):
            
            # compute output
            image = image.to(device)
            dem = dem.to(device)
            input = torch.cat((image, dem), dim=1) #[B, 4, 200, 200]
            output = model(input)
            
            if args.experts == 3:
                [many_output, medium_output, few_output] = output
                medium_output[:,many_index] = 0
                few_output[:,many_index + medium_index] = 0
                final_output = many_output + medium_output * m_scale + few_output * f_scale
                final_output[:,medium_index] /= 2
                final_output[:,few_index] /= 3
                
            if args.experts == 2:
                [many_output, few_output] = output
                few_output[:,many_index] = 0
                final_output = many_output + few_output * f_scale
                final_output[:,few_index] /= 2
            
            num_inputs = input.size(0)
            mask = mask.to(device)
            
            # measure accuracy
            preds = get_final_preds(final_output.detach().cpu().numpy())
            preds_many = get_final_preds(many_output.detach().cpu().numpy())
            preds_few = get_final_preds(few_output.detach().cpu().numpy())
            # gt = torch.squeeze(mask).detach().cpu().numpy()
            gt = mask.squeeze(0).detach().cpu().numpy()
            metrics.update(gt, preds)
            metrics_exp1.update(gt, preds_many)
            metrics_exp2.update(gt, preds_few)        
                
            # measure elapsed time
            batch_time.update(time.time() - end)
            end = time.time()
            
            if i % args.frequent == 0:
                msg = 'Test: [{0}/{1}]\t' \
                      'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'.format(
                          i, len(test_loader), batch_time=batch_time)

                logger.info(msg)
                
            if writer_dict:
                writer = writer_dict['logger']
                global_steps = writer_dict['vis_global_steps']

                # Pick a random image in the batch to visualize
                idx = np.random.randint(0, num_inputs)

                # Unnormalize the image to [0, 255] to visualize
                input_image = image.detach().cpu().numpy()[idx]
                input_image = input_image * test_dataset.std.reshape(3,1,1) + test_dataset.mean.reshape(3,1,1)
                input_image[input_image > 1.0] = 1.0
                input_image[input_image < 0.0] = 0.0

                ## Turn the numerical labels into colorful map
                mask_image = mask.detach().cpu().numpy()[idx].astype(np.int64)
                mask_image = vis_seg_mask(mask_image.squeeze())

                final_output = torch.nn.functional.softmax(final_output, dim=1)
                output_mask = torch.argmax(final_output, dim=1, keepdim=False)

                output_mask = output_mask.detach().cpu().numpy()[idx]
                output_mask = vis_seg_mask(output_mask)

                writer.add_image('input_image', input_image, global_steps,
                    dataformats='CHW')
                writer.add_image('result_vis', output_mask, global_steps,
                    dataformats='HWC')
                writer.add_image('gt_mask', mask_image, global_steps,
                    dataformats='HWC')

                writer_dict['vis_global_steps'] = global_steps + 1         
            
        mean_cls, mean_iou, acc_cls, overall_acc = metrics.get_scores()
        confusionMatrix = metrics.get_confusion_matrix()
        acc_ACE_many, acc_ACE_few = metrics.get_acc_cat()
        acc_exp1_many, acc_exp1_few = metrics_exp1.get_acc_cat()
        acc_exp2_many, acc_exp2_few = metrics_exp2.get_acc_cat()
        
        logger.info('Mean IoU score: {:.3f}'.format(mean_iou))
        logger.info('Mean accuracy: {:.3f}'.format(mean_cls))
        logger.info('Overall accuracy: {:.3f}'.format(overall_acc))
        logger.info('Many accuracy: {ACE:.3f}\t{expert1:.3f}\t{expert2:.3f}\t'.format(ACE=acc_ACE_many,
                                                                               expert1=acc_exp1_many,
                                                                               expert2=acc_exp2_many))
        logger.info('Few accuracy: {ACE:.3f}\t{expert1:.3f}\t{expert2:.3f}\t'.format(ACE=acc_ACE_few,
                                                                               expert1=acc_exp1_few,
                                                                               expert2=acc_exp2_few))
        classes = ["Background","Bedrock", "Bedrock with grass", "Large blocks", "Large blocks with grass", 
         "Scree", "Scree with grass", "Water area", "Forest", "Glacier"]
        for i in range(len(acc_cls)):
            logger.info(classes[i] + ' : {:.3f}'.format(acc_cls[i]))
        
    return confusionMatrix

class AverageMeter(object):
    """Computes and stores the average and current value."""
    
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count