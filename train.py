#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Retrain the YOLO model for your own dataset.
"""

import os, random, argparse
import numpy as np
import tensorflow.keras.backend as K
from tensorflow.keras.utils import multi_gpu_model
from tensorflow.keras.callbacks import TensorBoard, ModelCheckpoint, ReduceLROnPlateau, LearningRateScheduler, EarlyStopping, TerminateOnNaN, LambdaCallback
from tensorflow_model_optimization.sparsity import keras as sparsity
from yolo3.model import get_yolo3_train_model, get_optimizer
from yolo3.data import yolo3_data_generator_wrapper
from yolo2.model import get_yolo2_train_model
from yolo2.data import yolo2_data_generator_wrapper
from yolo3.utils import get_classes, get_anchors, get_dataset, optimize_tf_gpu

# Try to enable Auto Mixed Precision on TF 2.0
os.environ['TF_ENABLE_AUTO_MIXED_PRECISION'] = '1'
os.environ['TF_AUTO_MIXED_PRECISION_GRAPH_REWRITE_IGNORE_PERFORMANCE'] = '1'

import tensorflow as tf
optimize_tf_gpu(tf, K)


# some global value for lr scheduler
# need to update to CLI option in main()
lr_base = 1e-3
total_epochs = 250

def learning_rate_scheduler(epoch, curr_lr, mode='cosine_decay'):
    lr_power = 0.9
    lr = curr_lr

    # adam default lr
    if mode is 'adam':
        lr = 0.001

    # original lr scheduler
    if mode is 'power_decay':
        lr = lr_base * ((1 - float(epoch) / total_epochs) ** lr_power)

    # exponential decay policy
    if mode is 'exp_decay':
        lr = (float(lr_base) ** float(lr_power)) ** float(epoch + 1)

    # cosine decay policy, including warmup and hold stage
    if mode is 'cosine_decay':
        #warmup & hold hyperparams, adjust for your training
        warmup_epochs = 0
        hold_base_rate_epochs = 0
        warmup_lr = lr_base * 0.01
        lr = 0.5 * lr_base * (1 + np.cos(
             np.pi * float(epoch - warmup_epochs - hold_base_rate_epochs) /
             float(total_epochs - warmup_epochs - hold_base_rate_epochs)))

        if hold_base_rate_epochs > 0 and epoch < warmup_epochs + hold_base_rate_epochs:
            lr = lr_base

        if warmup_epochs > 0 and epoch < warmup_epochs:
            if lr_base < warmup_lr:
                raise ValueError('learning_rate_base must be larger or equal to '
                                 'warmup_learning_rate.')
            slope = (lr_base - warmup_lr) / float(warmup_epochs)
            warmup_rate = slope * float(epoch) + warmup_lr
            lr = warmup_rate

    if mode is 'progressive_drops':
        # drops as progression proceeds, good for sgd
        if epoch > 0.9 * total_epochs:
            lr = 0.0001
        elif epoch > 0.75 * total_epochs:
            lr = 0.001
        elif epoch > 0.5 * total_epochs:
            lr = 0.01
        else:
            lr = 0.1

    print('learning_rate change to: {}'.format(lr))
    return lr


def _main(args):
    global lr_base, total_epochs
    lr_base = args.learning_rate
    total_epochs = args.total_epoch

    annotation_file = args.annotation_file
    log_dir = 'logs/000/'
    classes_path = args.classes_path
    class_names = get_classes(classes_path)
    num_classes = len(class_names)

    anchors = get_anchors(args.anchors_path)
    num_anchors = len(anchors)

    # get freeze level according to CLI option
    if args.weights_path:
        freeze_level = 0
    else:
        freeze_level = 1

    if args.freeze_level is not None:
        freeze_level = args.freeze_level


    # callbacks for training process
    logging = TensorBoard(log_dir=log_dir, histogram_freq=0, write_graph=False, write_grads=False, write_images=False, update_freq='batch')
    checkpoint = ModelCheckpoint(log_dir + 'ep{epoch:03d}-loss{loss:.3f}-val_loss{val_loss:.3f}.h5',
        monitor='val_loss',
        verbose=1,
        save_weights_only=False,
        save_best_only=True,
        period=1)
    reduce_lr = ReduceLROnPlateau(monitor='val_loss', factor=0.5, patience=20, verbose=1, cooldown=0, min_lr=1e-10)
    lr_scheduler = LearningRateScheduler(learning_rate_scheduler)
    early_stopping = EarlyStopping(monitor='val_loss', min_delta=0, patience=50, verbose=1)
    terminate_on_nan = TerminateOnNaN()

    callbacks=[logging, checkpoint, reduce_lr, early_stopping, terminate_on_nan]

    # get train&val dataset
    dataset = get_dataset(annotation_file)
    if args.val_annotation_file:
        val_dataset = get_dataset(args.val_annotation_file)
        num_train = len(dataset)
        num_val = len(val_dataset)
        dataset.extend(val_dataset)
    else:
        val_split = args.val_split
        num_val = int(len(dataset)*val_split)
        num_train = len(dataset) - num_val

    # get different model type & train&val data generator
    if num_anchors == 9:
        # YOLOv3 use 9 anchors
        get_train_model = get_yolo3_train_model
        data_generator = yolo3_data_generator_wrapper
        tiny_version = False
    elif num_anchors == 6:
        # Tiny YOLOv3 use 6 anchors
        get_train_model = get_yolo3_train_model
        data_generator = yolo3_data_generator_wrapper
        tiny_version = True
    elif num_anchors == 5:
        # YOLOv2 use 5 anchors
        get_train_model = get_yolo2_train_model
        data_generator = yolo2_data_generator_wrapper
        tiny_version = False
    else:
        raise ValueError('Unsupported anchors number')


    # prepare model pruning config
    pruning_end_step = np.ceil(1.0 * num_train / args.batch_size).astype(np.int32) * args.total_epoch
    if args.model_pruning:
        pruning_callbacks = [sparsity.UpdatePruningStep(), sparsity.PruningSummaries(log_dir=log_dir, profile_batch=0)]
        callbacks = callbacks + pruning_callbacks

    # prepare optimizer
    optimizer = get_optimizer(args.optimizer, args.learning_rate)

    # get train model
    model = get_train_model(args.model_type, anchors, num_classes, weights_path=args.weights_path, freeze_level=freeze_level, optimizer=optimizer, label_smoothing=args.label_smoothing, model_pruning=args.model_pruning, pruning_end_step=pruning_end_step)
    # support multi-gpu training
    if args.gpu_num >= 2:
        model = multi_gpu_model(model, gpus=args.gpu_num)
        # recompile multi gpu model
        model.compile(optimizer=optimizer, loss={'yolo_loss': lambda y_true, y_pred: y_pred})
    model.summary()

    # Transfer training some epochs with frozen layers first if needed, to get a stable loss.
    input_shape = args.model_image_size
    assert (input_shape[0]%32 == 0 and input_shape[1]%32 == 0), 'Multiples of 32 required'
    initial_epoch = 0
    epochs = args.transfer_epoch
    print("Transfer training stage")
    print('Train on {} samples, val on {} samples, with batch size {}, input_shape {}.'.format(num_train, num_val, args.batch_size, input_shape))
    model.fit_generator(data_generator(dataset[:num_train], args.batch_size, input_shape, anchors, num_classes),
            steps_per_epoch=max(1, num_train//args.batch_size),
            validation_data=data_generator(dataset[num_train:], args.batch_size, input_shape, anchors, num_classes),
            validation_steps=max(1, num_val//args.batch_size),
            epochs=epochs,
            initial_epoch=initial_epoch,
            workers=1,
            use_multiprocessing=False,
            callbacks=callbacks)

    # Apply Cosine learning rate decay only after
    # unfreeze all layers
    if args.cosine_decay_learning_rate:
        callbacks.remove(reduce_lr)
        callbacks.append(lr_scheduler)

    # Unfreeze the whole network for further training
    # NOTE: more GPU memory is required after unfreezing the body
    print("Unfreeze and continue training, to fine-tune.")
    for i in range(len(model.layers)):
        model.layers[i].trainable = True
    model.compile(optimizer=optimizer, loss={'yolo_loss': lambda y_true, y_pred: y_pred}) # recompile to apply the change


    if args.multiscale:
        if args.model_type == 'yolo3_nano':
            raise ValueError("YOLOv3 nano model doesn't support multiscale training.")
        rescale_interval = args.rescale_interval
    else:
        rescale_interval = -1  #Doesn't rescale

    print('Train on {} samples, val on {} samples, with batch size {}, input_shape {}.'.format(num_train, num_val, args.batch_size, input_shape))
    model.fit_generator(data_generator(dataset[:num_train], args.batch_size, input_shape, anchors, num_classes, rescale_interval),
        steps_per_epoch=max(1, num_train//args.batch_size),
        validation_data=data_generator(dataset[num_train:], args.batch_size, input_shape, anchors, num_classes),
        validation_steps=max(1, num_val//args.batch_size),
        epochs=args.total_epoch,
        initial_epoch=epochs,
        workers=1,
        use_multiprocessing=False,
        callbacks=callbacks)

    # Finally store model
    if args.model_pruning:
        model = sparsity.strip_pruning(model)
    model.save(log_dir + 'trained_final.h5')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    # Model definition options
    parser.add_argument('--model_type', type=str, required=False, default='yolo3_mobilenet_lite',
        help='YOLO model type: yolo3_mobilenet_lite/tiny_yolo3_mobilenet/yolo3_darknet/..., default=yolo3_mobilenet_lite')
    parser.add_argument('--anchors_path', type=str, required=False, default='configs/yolo3_anchors.txt',
        help='path to anchor definitions, default=configs/yolo3_anchors.txt')
    parser.add_argument('--model_image_size', type=str,required=False, default='416x416',
        help = "Initial model image input size as <num>x<num>, default 416x416")
    parser.add_argument('--weights_path', type=str,required=False, default=None,
        help = "Pretrained model/weights file for fine tune")

    # Data options
    parser.add_argument('--annotation_file', type=str, required=False, default='trainval.txt',
        help='train annotation txt file, default=trainval.txt')
    parser.add_argument('--val_annotation_file', type=str, required=False, default=None,
        help='val annotation txt file, default=None')
    parser.add_argument('--val_split', type=float,required=False, default=0.1,
        help = "validation data persentage in dataset if no val dataset provide, default=0.1")
    parser.add_argument('--classes_path', type=str, required=False, default='configs/voc_classes.txt',
        help='path to class definitions, default=configs/voc_classes.txt')

    # Training options
    parser.add_argument('--batch_size', type=int,required=False, default=16,
        help = "Batch size for train, default=16")
    parser.add_argument('--optimizer', type=str,required=False, default='adam',
        help = "optimizer for training (adam/rmsprop/sgd), default=adam")
    parser.add_argument('--learning_rate', type=float,required=False, default=1e-3,
        help = "Initial learning rate, default=0.001")
    parser.add_argument('--cosine_decay_learning_rate', default=False, action="store_true",
        help='Whether to use cosine decay for learning rate control')
    parser.add_argument('--transfer_epoch', type=int,required=False, default=20,
        help = "Transfer training stage epochs, default=20")
    parser.add_argument('--freeze_level', type=int,required=False, default=None,
        help = "Freeze level of the model in transfer training stage. 0:NA/1:backbone/2:only open prediction layer")
    parser.add_argument('--total_epoch', type=int,required=False, default=250,
        help = "Total training epochs, default=250")
    parser.add_argument('--multiscale', default=False, action="store_true",
        help='Whether to use multiscale training')
    parser.add_argument('--rescale_interval', type=int, required=False, default=10,
        help = "Number of iteration(batches) interval to rescale input image, default=10")
    parser.add_argument('--model_pruning', default=False, action="store_true",
        help='Whether to use model pruning for optimization')
    parser.add_argument('--label_smoothing', type=float, required=False, default=0,
        help = "Label smoothing factor (between 0 and 1) for classification loss, default=0")
    parser.add_argument('--data_shuffle', default=False, action="store_true",
        help='Whether to shuffle train/val data for cross-validation')
    parser.add_argument('--gpu_num', type=int, required=False, default=1,
        help='Number of GPU to use, default=1')

    args = parser.parse_args()
    height, width = args.model_image_size.split('x')
    args.model_image_size = (int(height), int(width))

    _main(args)
