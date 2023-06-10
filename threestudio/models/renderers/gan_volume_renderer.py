from dataclasses import dataclass

import nerfacc
import torch
import torch.nn.functional as F

import threestudio
from threestudio.models.background.base import BaseBackground
from threestudio.models.geometry.base import BaseImplicitGeometry
from threestudio.models.materials.base import BaseMaterial
from threestudio.models.renderers.base import VolumeRenderer
from threestudio.models.renderers.nerf_volume_renderer import NeRFVolumeRenderer
from threestudio.utils.base import BaseModule
from threestudio.utils.misc import get_device
from threestudio.utils.ops import chunk_batch
from threestudio.utils.rasterize import NVDiffRasterizerContext
from threestudio.utils.typing import *
from threestudio.utils.GAN.mobilenet import MobileNetV3 as GlobalEncoder
from threestudio.utils.GAN.vae import Decoder as Generator
from threestudio.utils.GAN.vae import Encoder as LocalEncoder
from threestudio.utils.GAN.distribution import DiagonalGaussianDistribution
from threestudio.utils.GAN.discriminator import NLayerDiscriminator, weights_init


@threestudio.register("gan-volume-renderer")
class GANVolumeRenderer(VolumeRenderer):
    @dataclass
    class Config(VolumeRenderer.Config):
        num_samples_per_ray: int = 512
        randomized: bool = True
        eval_chunk_size: int = 160000
        grid_prune: bool = True
        return_comp_normal: bool = False
        return_normal_perturb: bool = False
    
    cfg: Config
    def configure(
        self,
        geometry: BaseImplicitGeometry,
        material: BaseMaterial,
        background: BaseBackground,
    ) -> None:
        self.base_renderer = NeRFVolumeRenderer(self.cfg, geometry, material, background)
        self.generator = Generator(ch=64, out_ch=3, ch_mult=(1,2,4), num_res_blocks=2,
                 attn_resolutions=[], dropout=0.0, resamp_with_conv=True, in_channels=7,
                 resolution=512, z_channels=16)
        self.local_encoder = LocalEncoder(ch=32, out_ch=3, ch_mult=(1,2,4), num_res_blocks=1,
                 attn_resolutions=[], dropout=0.0, resamp_with_conv=True, in_channels=3,
                 resolution=512, z_channels=16)
        self.global_encoder = GlobalEncoder(n_class=64)
        self.discriminator = NLayerDiscriminator(input_nc=6,
            n_layers=3, use_actnorm=False, ndf=64
        ).apply(weights_init)

    def forward(
        self,
        rays_o: Float[Tensor, "B H W 3"],
        rays_d: Float[Tensor, "B H W 3"],
        light_positions: Float[Tensor, "B 3"],
        bg_color: Optional[Tensor] = None,
        gt_rgb: Float[Tensor, "B H W 3"] = None,
        multi_level_guidance: Bool = False,
        **kwargs
    ) -> Dict[str, Float[Tensor, "..."]]:
        B, H, W, _ = rays_o.shape
        if gt_rgb is not None and multi_level_guidance:
            generator_level = torch.randint(0, 3, (1,)).item()
            # interval = torch.randint(0, 4, (1,)).item()
            # rays_o = rays_o[:, interval::4, interval::4]
            # rays_d = rays_d[:, interval::4, interval::4]
        else:
            generator_level = 0
        rays_o = torch.nn.functional.interpolate(
            rays_o.permute(0, 3, 1, 2), (H // 4, W // 4), mode='bilinear').permute(0, 2, 3, 1)
        rays_d = torch.nn.functional.interpolate(
            rays_d.permute(0, 3, 1, 2), (H // 4, W // 4), mode='bilinear').permute(0, 2, 3, 1)
        out = self.base_renderer(rays_o, rays_d, light_positions, bg_color, **kwargs)
        # if gt_rgb is not None and multi_level_guidance:
        #     out["comp_gt_rgb"] = gt_rgb[:, interval::4, interval::4]
        out["comp_gt_rgb"] = gt_rgb
        comp_rgb = out["comp_rgb"][..., :3]
        latent = out["comp_rgb"][..., 3:]
        out["comp_lr_rgb"] = comp_rgb.clone()

        posterior = DiagonalGaussianDistribution(latent.permute(0, 3, 1, 2))
        z_map = posterior.sample()
        # z_map = posterior.mode()
        lr_rgb = comp_rgb.permute(0, 3, 1, 2).detach()

        if generator_level == 0:
            g_code_rgb = self.global_encoder(F.interpolate(lr_rgb, (224, 224)))
            comp_gan_rgb = self.generator(torch.cat([lr_rgb, z_map], dim=1), g_code_rgb)
        elif generator_level == 1:
            g_code_rgb = self.global_encoder(F.interpolate(gt_rgb.permute(0, 3, 1, 2), (224, 224)))
            comp_gan_rgb = self.generator(torch.cat([lr_rgb, z_map], dim=1), g_code_rgb)
        elif generator_level == 2:
            g_code_rgb = self.global_encoder(F.interpolate(gt_rgb.permute(0, 3, 1, 2), (224, 224)))
            l_code_rgb = self.local_encoder(gt_rgb.permute(0, 3, 1, 2))
            posterior = DiagonalGaussianDistribution(l_code_rgb)
            z_map = posterior.sample()
            # z_map = posterior.mode()
            comp_gan_rgb = self.generator(torch.cat([lr_rgb, z_map], dim=1), g_code_rgb)

        comp_rgb = F.interpolate(comp_rgb.permute(0, 3, 1, 2), (H, W), mode='bilinear')
        out.update({
            "posterior": posterior,
            "comp_gan_rgb": comp_gan_rgb.permute(0, 2, 3, 1),
            "comp_rgb": comp_rgb.permute(0, 2, 3, 1),
            "generator_level": generator_level
        })
        return out

    def update_step(
        self, epoch: int, global_step: int, on_load_weights: bool = False
    ) -> None:
        self.base_renderer.update_step(epoch, global_step, on_load_weights)

    def train(self, mode=True):
        return self.base_renderer.train(mode)

    def eval(self):
        return self.base_renderer.eval()
