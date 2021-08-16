import paddle
import paddle.nn as nn
from paddle.nn import layer
import paddle.nn.functional as F

from paddleseg.models import layers
from paddleseg.cvlibs import manager
from paddleseg.utils import utils


@manager.MODELS.add_component
class UPerNet(nn.Layer):
    """ 
    Unified Perceptual Parsing for Scene Understanding
    (https://arxiv.org/abs/1807.10221)
    The UPerNet implementation based on PaddlePaddle.
    Args:
        num_classes (int): The unique number of target classes.
        backbone (Paddle.nn.Layer): Backbone network, currently support Resnet50/101.
        backbone_indices (tuple): Four values in the tuple indicate the indices of output of backbone.
        enable_auxiliary_loss (bool, optional): A bool value indicates whether adding auxiliary loss. Default: False.
        align_corners (bool, optional): An argument of F.interpolate. It should be set to False when the feature size is even,
            e.g. 1024x512, otherwise it is True, e.g. 769x769. Default: False.
        pretrained (str, optional): The path or url of pretrained model. Default: None.
    """

    def __init__(self,
                 num_classes,
                 backbone,
                 backbone_indices,
                 channels,
                 enable_auxiliary_loss=False,
                 align_corners=False,
                 dropout_ratio=0.1,
                 pretrained=None):
        super(UPerNet, self).__init__()
        self.backbone = backbone
        self.backbone_indices = backbone_indices
        self.in_channels = [
            self.backbone.feat_channels[i] for i in backbone_indices
        ]
        self.align_corners = align_corners
        self.pretrained = pretrained
        self.enable_auxiliary_loss = enable_auxiliary_loss
        if self.backbone.layers == 18:
            fpn_dim = 128
            inplane_head = 512
            fpn_inplanes = [64, 128, 256, 512]
        else:
            fpn_dim = 256
            inplane_head = 2048
            fpn_inplanes = [256, 512, 1024, 2048]

        self.head = UPerNetHead(
            inplane=inplane_head,
            num_class=num_classes,
            fpn_inplanes=fpn_inplanes,
            dropout_ratio=dropout_ratio,
            channels=channels,
            fpn_dim=fpn_dim,
            enable_auxiliary_loss=self.enable_auxiliary_loss)
        self.init_weight()

    def forward(self, x):
        feats = self.backbone(x)
        feats = [feats[i] for i in self.backbone_indices]
        logit_list = self.head(feats)
        logit_list = [
            F.interpolate(
                logit,
                paddle.shape(x)[2:],
                mode='bilinear',
                align_corners=self.align_corners) for logit in logit_list
        ]
        return logit_list

    def init_weight(self):
        if self.pretrained is not None:
            utils.load_entire_model(self, self.pretrained)


class UPerNetHead(nn.Layer):
    """
    The UPerNetHead implementation.

    Args:
        inplane (int): Input channels of PPM module.
        num_class (int): The unique number of target classes.
        fpn_inplanes (list): The feature channels from backbone.
        fpn_dim (int, optional): The input channels of FPN module. Default: 512.
        enable_auxiliary_loss (bool, optional): A bool value indicates whether adding auxiliary loss. Default: False.
    """

    def __init__(self,
                 inplane,
                 num_class,
                 fpn_inplanes,
                 channels,
                 dropout_ratio=0.1,
                 fpn_dim=512,
                 enable_auxiliary_loss=False):
        super(UPerNetHead, self).__init__()
        self.ppm = layers.PPModule(
            in_channels=inplane,
            out_channels=fpn_dim,
            bin_sizes=(1, 2, 3, 6),
            dim_reduction=True,
            align_corners=True)
        self.enable_auxiliary_loss = enable_auxiliary_loss
        self.lateral_convs = []
        self.fpn_out = []

        for fpn_inplane in fpn_inplanes[:-1]:
            self.lateral_convs.append(
                nn.Sequential(
                    nn.Conv2D(fpn_inplane, fpn_dim, 1),
                    layers.SyncBatchNorm(fpn_dim), nn.ReLU()))
            self.fpn_out.append(
                nn.Sequential(
                    layers.ConvBNReLU(fpn_dim, fpn_dim, 3, bias_attr=False)))

        self.lateral_convs = nn.LayerList(self.lateral_convs)
        self.fpn_out = nn.LayerList(self.fpn_out)

        if self.enable_auxiliary_loss:
            if dropout_ratio is not None:
                self.dsn = nn.Sequential(
                    layers.ConvBNReLU(fpn_inplanes[2], fpn_inplanes[2], 3, padding=1),
                    nn.Dropout2D(dropout_ratio),
                    nn.Conv2D(fpn_inplanes[2], num_class, kernel_size=1))
            else:
                self.dsn = nn.Sequential(
                    layers.ConvBNReLU(fpn_inplanes[2], fpn_inplanes[2], 3, padding=1),
                    nn.Conv2D(fpn_inplanes[2], num_class, kernel_size=1))
        
        if dropout_ratio is not None:
            self.dropout = nn.Dropout2D(dropout_ratio)
        else:
            self.dropout = None
        self.fpn_bottleneck = layers.ConvBNReLU(
            len(fpn_inplanes) * channels,
            channels,
            3,
            padding=1)

        self.conv_last = nn.Sequential(
            layers.ConvBNReLU(
                len(fpn_inplanes) * fpn_dim, fpn_dim, 3, bias_attr=False),
            nn.Conv2D(fpn_dim, num_class, kernel_size=1))
        self.conv_seg = nn.Conv2D(channels, num_class, kernel_size=1)

    def cls_seg(self, feat):
        if self.dropout is not None:
            feat = self.dropout(feat)
        output = self.conv_seg(feat)
        return output

    def forward(self, conv_out):
        psp_out = self.ppm(conv_out[-1])
        f = psp_out
        fpn_feature_list = [psp_out]
        out = []
        for i in reversed(range(len(conv_out) - 1)):
            conv_x = conv_out[i]
            conv_x = self.lateral_convs[i](conv_x)
            prev_shape = paddle.shape(conv_x)[2:]
            f = conv_x + F.interpolate(f, prev_shape, mode='bilinear', align_corners=True)
            fpn_feature_list.append(self.fpn_out[i](f))
        fpn_feature_list.reverse()
        output_size = fpn_feature_list[0].shape[2:]
        # resize multi-scales feature
        for index in range(len(conv_out)-1, 0, -1):
            fpn_feature_list[index] = F.interpolate(
                fpn_feature_list[index],
                size=output_size,
                mode='bilinear',
                align_corners=True
            )
        fusion_out = paddle.concat(fpn_feature_list, 1)
        x = self.fpn_bottleneck(fusion_out)
        x = self.cls_seg(x)
        if self.enable_auxiliary_loss:
            dsn = self.dsn(conv_out[2])
            out.append(x)
            out.append(dsn)
            return out
        else:
            return [x]
