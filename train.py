"""
Trains a Pixel-CNN++ generative model on CIFAR-10 or Tiny ImageNet data.
Uses multiple GPUs, indicated by the flag --nr_gpu

Example usage:
CUDA_VISIBLE_DEVICES=0,1,2,3 python train_double_cnn.py --nr_gpu 4
"""

import os
import sys
import json
import argparse
import time

import numpy as np
import tensorflow as tf

from pixel_cnn_pp import nn
from pixel_cnn_pp.model import model_spec
from utils import plotting

# -----------------------------------------------------------------------------
parser = argparse.ArgumentParser()
# data I/O
parser.add_argument('-i', '--data_dir', type=str, default='data/', help='Location for the dataset')
parser.add_argument('-o', '--save_dir', type=str, default='logs/', help='Location for parameter checkpoints and samples')
parser.add_argument('-d', '--data_set', type=str, default='cifar100', help='Can be either cifar100|cifar10|imagenet')
parser.add_argument('-t', '--save_interval', type=int, default=20, help='Every how many epochs to write checkpoint/samples?')
parser.add_argument('-r', '--load_params', dest='load_params', action='store_true', help='Restore training from previous model checkpoint?')
# model
parser.add_argument('-q', '--nr_resnet', type=int, default=5, help='Number of residual blocks per stage of the model')
parser.add_argument('-n', '--nr_filters', type=int, default=160, help='Number of filters to use across the model. Higher = larger model.')
parser.add_argument('-m', '--nr_logistic_mix', type=int, default=10, help='Number of logistic components in the mixture. Higher = more flexible model')
parser.add_argument('-z', '--resnet_nonlinearity', type=str, default='concat_elu', help='Which nonlinearity to use in the ResNet layers. One of "concat_elu", "elu", "relu" ')
parser.add_argument('-c', '--class_conditional', dest='class_conditional', action='store_true', help='Condition generative model on labels?')
parser.add_argument('-ed', '--energy_distance', dest='energy_distance', action='store_true', help='use energy distance in place of likelihood')
# optimization
parser.add_argument('-l', '--learning_rate', type=float, default=0.001, help='Base learning rate')
parser.add_argument('-e', '--lr_decay', type=float, default=0.999995, help='Learning rate decay, applied every step of the optimization')
parser.add_argument('-b', '--batch_size', type=int, default=16, help='Batch size during training per GPU')
parser.add_argument('-u', '--init_batch_size', type=int, default=16, help='How much data to use for data-dependent initialization.')
parser.add_argument('-p', '--dropout_p', type=float, default=0.5, help='Dropout strength (i.e. 1 - keep_prob). 0 = No dropout, higher = more dropout.')
parser.add_argument('-x', '--max_epochs', type=int, default=5000, help='How many epochs to run in total?')
# evaluation
parser.add_argument('--polyak_decay', type=float, default=0.9995, help='Exponential decay rate of the sum of previous model iterates during Polyak averaging')
parser.add_argument('-ns', '--num_samples', type=int, default=1, help='How many batches of samples to output.')
# reproducibility
parser.add_argument('-s', '--seed', type=int, default=1, help='Random seed to use')
args = parser.parse_args()
print('input args:\n', json.dumps(vars(args), indent=4, separators=(',',':'))) # pretty print args

# -----------------------------------------------------------------------------
# fix random seed for reproducibility
rng = np.random.RandomState(args.seed)
tf.set_random_seed(args.seed)

# energy distance or maximum likelihood?
if args.energy_distance:
    loss_fun = nn.energy_distance
else:
    loss_fun = nn.discretized_mix_logistic_loss

# initialize data loaders for train/test splits
if args.data_set == 'imagenet' and args.class_conditional:
    raise("We currently don't have labels for the small imagenet data set")
if args.data_set == 'cifar10':
    import data.cifar10_data as cifar10_data
    DataLoader = cifar10_data.DataLoader
elif args.data_set == 'cifar100':
    import data.cifar100_data as cifar100_data
    DataLoader = cifar100_data.DataLoader
elif args.data_set == 'imagenet':
    import data.imagenet_data as imagenet_data
    DataLoader = imagenet_data.DataLoader
else:
    raise("unsupported dataset")
train_data = DataLoader(args.data_dir, 'train', args.batch_size, rng=rng, shuffle=True, return_labels=args.class_conditional)
test_data = DataLoader(args.data_dir, 'test', args.batch_size, shuffle=False, return_labels=args.class_conditional)
obs_shape = train_data.get_observation_size() # e.g. a tuple (32,32,3)
assert len(obs_shape) == 3, 'assumed right now'

# data place holders
x_init = tf.placeholder(tf.float32, shape=(args.init_batch_size,) + obs_shape)
xs = tf.placeholder(tf.float32, shape=(args.batch_size, ) + obs_shape)

# if the model is class-conditional we'll set up label placeholders + one-hot encodings 'h' to condition on
if args.class_conditional:
    num_labels = train_data.get_num_labels()
    y_init = tf.placeholder(tf.int32, shape=(args.init_batch_size,))
    h_init = tf.one_hot(y_init, num_labels)
    ys = tf.placeholder(tf.int32, shape=(args.batch_size,))
    hs = tf.one_hot(ys, num_labels)
else:
    h_init = None
    hs = None

# create the model
model_opt = { 'nr_resnet': args.nr_resnet, 'nr_filters': args.nr_filters, 'nr_logistic_mix': args.nr_logistic_mix, 'resnet_nonlinearity': args.resnet_nonlinearity, 'energy_distance': args.energy_distance }
model = tf.make_template('model', model_spec)

# run once for data dependent initialization of parameters
init_pass = model(x_init, h_init, init=True, dropout_p=args.dropout_p, **model_opt)

# keep track of moving average
all_params = tf.trainable_variables()
ema = tf.train.ExponentialMovingAverage(decay=args.polyak_decay)
maintain_averages_op = tf.group(ema.apply(all_params))
ema_params = [ema.average(p) for p in all_params]

# train
out = model(xs, hs, ema=None, dropout_p=args.dropout_p, **model_opt)
loss_gen = loss_fun(tf.stop_gradient(xs), out)

# gradients
grads = tf.gradients(loss_gen, all_params, colocate_gradients_with_ops=True)

# test
out = model(xs, hs, ema=ema, dropout_p=0., **model_opt)
loss_gen_test = loss_fun(xs, out)

# sample
out = model(xs, hs, ema=ema, dropout_p=0, **model_opt)
if args.energy_distance:
    new_x_gen = out[0]
else:
    new_x_gen = nn.sample_from_discretized_mix_logistic(out, args.nr_logistic_mix)

# add losses and gradients together and get training updates
tf_lr = tf.placeholder(tf.float32, shape=[])
# training op
optimizer = tf.group(nn.adam_updates(all_params, grads, lr=tf_lr, mom1=0.95, mom2=0.9995), maintain_averages_op)

# convert loss to bits/dim
bits_per_dim = loss_gen/(np.log(2.)*np.prod(obs_shape))
bits_per_dim_test = loss_gen_test/(np.log(2.)*np.prod(obs_shape))

tf.summary.scalar("Loss", loss_gen / args.batch_size)
tf.summary.scalar("BPD", bits_per_dim / args.batch_size)
tf.summary.scalar("Learning rate", tf_lr)
summary = tf.summary.merge_all()

# sample from the model
def sample_from_model(sess):
    x_gen = np.zeros((args.batch_size,) + obs_shape, dtype=np.float32)
    for yi in range(obs_shape[0]):
        for xi in range(obs_shape[1]):
            new_x_gen_np = sess.run(new_x_gen, {xs: x_gen})
            x_gen[:,yi,xi,:] = new_x_gen_np[:,yi,xi,:]
    return x_gen

# init & save
initializer = tf.global_variables_initializer()
saver = tf.train.Saver()

# turn numpy inputs into feed_dict for use with tensorflow
def make_feed_dict(data, init=False):
    if type(data) is tuple:
        x,y = data
    else:
        x = data
        y = None
    x = np.cast[np.float32]((x - 127.5) / 127.5) # input to pixelCNN is scaled from uint8 [0,255] to float in range [-1,1]
    if init:
        feed_dict = {x_init: x}
        if y is not None:
            feed_dict.update({y_init: y})
    else:
        feed_dict = {xs: x}
        if y is not None:
            feed_dict.update({ys: y})
    return feed_dict

# //////////// perform training //////////////
if not os.path.exists(args.save_dir):
    os.makedirs(args.save_dir)
test_bpd = []
lr = args.learning_rate
with tf.Session() as sess:
    writer = tf.summary.FileWriter(args.save_dir + '/train', sess.graph)
    test_writer = tf.summary.FileWriter(args.save_dir + '/test', sess.graph)
    for epoch in range(args.max_epochs):
        begin = time.time()

        # init
        if epoch == 0:
            train_data.reset()  # rewind the iterator back to 0 to do one full epoch
            if args.load_params:
                ckpt_file = args.save_dir + '/params_' + args.data_set + '.ckpt'
                print('restoring parameters from', ckpt_file)
                saver.restore(sess, ckpt_file)
            else:
                print('initializing the model...')
                sess.run(initializer)
                feed_dict = make_feed_dict(train_data.next(args.init_batch_size), init=True)  # manually retrieve exactly init_batch_size examples
                sess.run(init_pass, feed_dict)
            print('starting training')

        # train for one epoch
        train_losses = []
        for d in train_data:
            feed_dict = make_feed_dict(d)
            # forward/backward/update model on each gpu
            lr *= args.lr_decay
            feed_dict.update({ tf_lr: lr })
            sum_, l,_ = sess.run([summary, bits_per_dim, optimizer], feed_dict)
            writer.add_summary(sum_, counter)
            train_losses.append(l)

        train_loss_gen = np.mean(train_losses)

        # compute likelihood over test data
        test_losses = []
        for d in test_data:
            feed_dict = make_feed_dict(d)
            l = sess.run(bits_per_dim_test, feed_dict)
            test_losses.append(l)
        test_loss_gen = np.mean(test_losses)
        test_bpd.append(test_loss_gen)

        test_summary = tf.Summary(value=[
            tf.Summary.Value(tag="BPD", simple_value=test_loss_gen / args.batch_size)])
        test_writer.add_summary(test_summary, counter)


        # log progress to console
        print("Iteration %d, time = %ds, train bits_per_dim = %.4f, test bits_per_dim = %.4f" % (
            epoch, time.time()-begin, train_loss_gen / args.batch_size, test_loss_gen / args.batch_size))
        sys.stdout.flush()


        # generate samples from the model
        sample_x = []
        for i in range(args.num_samples):
            sample_x.append(sample_from_model(sess))
        sample_x = np.concatenate(sample_x,axis=0)
        img_tile = plotting.img_tile(sample_x[:100], aspect_ratio=1.0, border_color=1.0, stretch=True)
        img = plotting.plot_img(img_tile, title=args.data_set + ' samples')
        plotting.plt.savefig(os.path.join(args.save_dir,'%s_sample%d.png' % (args.data_set, epoch)))
        plotting.plt.close('all')
        # save params
        saver.save(sess, args.save_dir + '/params_' + args.data_set + '.ckpt')
