from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf

_BATCH_NORM_DECAY = 0.977
_BATCH_NORM_EPSILON = 1e-5

def batch_norm_relu(inputs, is_training, data_format):
    """Performs a batch normalization followed by a ReLU

    """

    inputs = tf.layers.batch_normalization(inputs=inputs,
                                           axis=1 if data_format == 'channel_first' else 3,
                                           momentum=_BATCH_NORM_DECAY, epsilon=_BATCH_NORM_EPSILON,
                                           scale=True, training=is_training, fused=True)
    inputs = tf.nn.relu(inputs)

    return inputs

def fixed_padding(inputs, kernel_size, data_format):
    """Pads the input along the spatial dimensions indepently of input size

    Args:
        inputs: A tensor of size [batch, channels, height_in, width_in] or [batch,
        height_in, width_in, channels] depending on data_format.
        kernel_size: The kernel to be used in the conv2d or max_pool2d operation
        data_format: The input format('channels_last' or 'channels_first')

    Returns:
        A tensor with the same format as the input with the data either intact(if
        kernel_size == 1) or padded (if kernel_size > 1)
    """

    pad_total = kernel_size - 1
    pad_beg = pad_total // 2
    pad_end = pad_total - pad_beg

    if data_format == 'channels_first':
        padded_inputs = tf.pad(inputs, [[0, 0], [0, 0],
                                        [pad_beg, pad_end], [pad_beg, pad_end]])
    else:
        padded_inputs = tf.pad(inputs, [[0, 0], [pad_beg, pad_end],
                                        [pad_beg, pad_end], [0, 0]])

    return padded_inputs

def conv2d_fixed_padding(inputs, filters, kernel_size, strides, data_format):
    """Strided 2-D convolution with explicit padding"""

    if strides > 1:
        inputs = fixed_padding(inputs, kernel_size, data_format)

    return tf.layers.conv2d(inputs=inputs,
                            filters=filters,
                            kernel_size=kernel_size,
                            strides=strides,
                            padding=('SAME' if strides == 1 else 'VALID'),
                            use_bias=False,
                            kernel_initializer=tf.variance_scaling_initializer(),
                            data_format=data_format)


def building_block(inputs, filters, is_training, projection_shortcut, strides,
                   data_format):
    """Standard building block for residual networks with BN before convolutions

    Args:
        input: A tensor of size [batch, channels, height_in, width_in] or [batch,
        height_in, width_in, channels] depending on data_format.
        filters: The number of filters for the convolutions.
        is_training: A boolean for whether the model is training or inference mode. Needed
        for batch normalization.
        projection_shortcut: The function to use for projection shortcuts(typically
        a 1 x 1 convolution when downsampling the input).
        strides: The block's stride. If greater than 1, this block will ultimately
        downsample the input.
        data_format: The input format('channels_last' or 'channels_first')

    Returns:
        The output tensor of the block.
    """

    shortcut = inputs
    inputs = batch_norm_relu(inputs, is_training, data_format)

    # The projection shortcut should come after the first batch norm and ReLU
    # since it performs a 1x1 convolution.

    if projection_shortcut is not None:
        shortcut = projection_shortcut(inputs)

    inputs = conv2d_fixed_padding(inputs=inputs, filters=filters,
                                  kernel_size=3, strides=strides,
                                  data_format=data_format)
    inputs = batch_norm_relu(inputs, is_training, data_format)

    inputs = conv2d_fixed_padding(inputs=inputs, filters=filters,
                                  kernel_size=3, strides=1,
                                  data_format=data_format)

    return inputs + shortcut

def bottleneck_block(inputs, filters, is_training, projection_shortcut,
                     strides, data_format):
    """Bottleneck block variant for residual networks with BN before convolution.

    """

    shortcut = inputs
    inputs = batch_norm_relu(inputs, is_training, data_format)

    if projection_shortcut is not None:
        shortcut = projection_shortcut(inputs)

    inputs = conv2d_fixed_padding(inputs=inputs, filters=filters, kernel_size=1,
                                  strides=1, data_format=data_format)
    inputs = batch_norm_relu(inputs, is_training, data_format)

    inputs = conv2d_fixed_padding(inputs=inputs, filters=filters, kernel_size=3,
                                  strides=strides, data_format=data_format)
    inputs = batch_norm_relu(inputs, is_training, data_format)
    inputs = conv2d_fixed_padding(inputs, filters=4*filters, kernel_size=1,
                                  strides=1, data_format=data_format)

    return inputs + shortcut

def block_layer(inputs, filters, block_fn, blocks, strides, is_training, name, data_format):
    """Creates one layer of  blocks for the ResNet model.

    Args:
        inputs: A tensor of size [batch, channels, height_in, width_in] or [batch,
        height_in, width_in, channels] depending on data_format.
        filters: The number of filters for the first convolution of the layer
        block_fn: The block to use within the model, either "building block" or "bottleneck_block"
        blocks: The number of blocks contained in the layer
        strides: The stride to use for the first convolution of the layer. If greater
        than 1, this layer will ultimately downsample the input.
        is_training: Either True of False, whether we are currently training the model.
        Needed for batch norm.
        name: A string name for the tensor output of the block layer.
        data_format: The input format('channels_last' or 'channels_first')
    """

    # Bottleneck blocks end with 4x the number of filters as they start with
    filters_out = 4 * filters if block_fn is bottleneck_block else filters


    def projection_shortcut(inputs):
        return conv2d_fixed_padding(inputs=inputs, filters=filters_out,
                                    kernel_size=1, strides=strides,
                                    data_format=data_format)
    # Only the first block per block layer uses projection_shortcut and strides

    inputs = block_fn(inputs, filters, is_training, projection_shortcut,
                      strides, data_format)

    for _ in range(1, blocks):
        inputs = block_fn(inputs, filters, is_training, None, 1, data_format)

    return tf.identity(inputs, name)

def cifar10_resnet_v2_generator(resnet_size, num_classes, data_format=None):
    """Generator for CIFAR-10 ResNet v2 models

    Args:
        resnet_size: A single integer for the size of the ResNet model.
        num_classes: The number of possible classes for image classification
        data_format: The input format ('channels_last', 'channels_first', or None)
            If set to None, the format is dependent on whether a GPU is available.

    Returns:
        The model function that takes in 'inputs' and 'is_training' and returns the
        output tensor of the ResNet model.
    """

    if resnet_size % 6 != 2:
        raise ValueError('resnet_size must be 6n + 2:', resnet_size)

    num_blocks = (resnet_size - 2) // 6

    if data_format is None:
        data_format = ('channels_first' if tf.test.is_built_with_cuda() else 'channels_last')

    def model(inputs, is_training):
        """Constructs the ResNet model given the inputs."""
        if data_format == 'channels_first':
            inputs = tf.transpose(inputs, [0, 3, 1, 2])

        inputs = conv2d_fixed_padding(inputs=inputs, filters=16, kernel_size=3,
                                      strides=1, data_format=data_format)


        inputs = tf.identity(inputs, 'initial_conv')

        inputs = block_layer(inputs=inputs, filters=16, block_fn=building_block,
                             blocks=num_blocks, strides=1, is_training=is_training,
                             name='block_layer1', data_format=data_format)

        inputs = block_layer(inputs=inputs, filters=32, block_fn=building_block,
                             blocks=num_blocks, strides=2, is_training=is_training,
                             name='block_layer2', data_format=data_format)

        inputs = block_layer(inputs=inputs, filters=64, block_fn=building_block,
                             blocks=num_blocks, strides=2, is_training=is_training,
                             name='block_layer3', data_format=data_format)

        inputs = batch_norm_relu(inputs, is_training, data_format)

        inputs = tf.layers.average_pooling2d(inputs=inputs,
                                             pool_size=8, strides=1, padding='VALID',
                                             data_format=data_format)

        inputs = tf.identity(inputs, 'final_avg_pool')
        inputs = tf.reshape(inputs, [-1, 64])
        inputs = tf.layers.dense(inputs=inputs, units=num_classes)

        inputs = tf.identity(inputs, 'final_dense')

        return inputs

    return model



