import json
import math
import os
from dataclasses import dataclass

import cv2
import numpy as np
import torch
from tqdm import tqdm

import threestudio
from threestudio.models.geometry.gaussian import BasicPointCloud
from threestudio.systems.base import BaseLift3DSystem
from threestudio.systems.utils import parse_optimizer, parse_scheduler
from threestudio.utils.GAN.loss import discriminator_loss, generator_loss
from threestudio.utils.gaussian.loss import l1_loss, ssim
from threestudio.utils.misc import cleanup, get_device
from threestudio.utils.perceptual import PerceptualLoss
from threestudio.utils.typing import *


def convert_pose(C2W):
    flip_yz = np.eye(4)
    # flip_yz[1, 1] = -1
    flip_yz[2, 2] = -1
    C2W = np.matmul(C2W, flip_yz)
    return C2W


def getProjectionMatrix(znear, zfar, fovX, fovY):
    tanHalfFovY = math.tan((fovY / 2))
    tanHalfFovX = math.tan((fovX / 2))

    top = tanHalfFovY * znear
    bottom = -top
    right = tanHalfFovX * znear
    left = -right

    P = torch.zeros(4, 4)

    z_sign = 1.0

    P[0, 0] = 2.0 * znear / (right - left)
    P[1, 1] = 2.0 * znear / (top - bottom)
    P[0, 2] = (right + left) / (right - left)
    P[1, 2] = (top + bottom) / (top - bottom)
    P[3, 2] = z_sign
    P[2, 2] = z_sign * zfar / (zfar - znear)
    P[2, 3] = -(zfar * znear) / (zfar - znear)
    return P


def getFOV(P, znear, zfar):
    right = znear / P[0, 0]
    top = znear / P[1, 1]
    tanHalfFovX = right / znear
    tanHalfFovY = top / znear
    fovY = math.atan(tanHalfFovY) * 2
    fovX = math.atan(tanHalfFovX) * 2
    return fovX, fovY


def get_cam_info(c2w, fovx, fovy):
    c2w = c2w[0].cpu().numpy()
    c2w = convert_pose(c2w)
    world_view_transform = np.linalg.inv(c2w)

    world_view_transform = (
        torch.tensor(world_view_transform).transpose(0, 1).cuda().float()
    )
    projection_matrix = (
        getProjectionMatrix(znear=0.01, zfar=100.0, fovX=fovx, fovY=fovy)
        .transpose(0, 1)
        .cuda()
    )
    full_proj_transform = (
        world_view_transform.unsqueeze(0).bmm(projection_matrix.unsqueeze(0))
    ).squeeze(0)
    camera_center = world_view_transform.inverse()[3, :3]

    return world_view_transform, full_proj_transform, camera_center


class Camera(NamedTuple):
    FoVx: torch.Tensor
    FoVy: torch.Tensor
    camera_center: torch.Tensor
    image_width: int
    image_height: int
    world_view_transform: torch.Tensor
    full_proj_transform: torch.Tensor


@threestudio.register("dynamic-gaussian-splatting-instruct-system")
class DynamicGaussianSplattingInstruct(BaseLift3DSystem):
    @dataclass
    class Config(BaseLift3DSystem.Config):
        extent: float = 5.0
        num_pts: int = 100
        invert_bg_prob: float = 0.5

        per_editing_step: int = 10
        start_editing_step: int = 1000

        get_patch_size: int = 512
        edit_path: str = ""

    cfg: Config

    def configure(self) -> None:
        # set up geometry, material, background, renderer
        super().configure()
        self.perceptual_loss = PerceptualLoss().eval().to()
        self.automatic_optimization = False

        self.background_tensor = torch.tensor(
            [0, 0, 0], dtype=torch.float32, device="cuda"
        )
        # Since this data set has no colmap data, we start with random points
        num_pts = self.cfg.num_pts

        self.extent = self.cfg.extent

        if len(self.geometry.cfg.geometry_convert_from) == 0:
            print(f"Generating random point cloud ({num_pts})...")
            phis = np.random.random((num_pts,)) * 2 * np.pi
            costheta = np.random.random((num_pts,)) * 2 - 1
            thetas = np.arccos(costheta)
            mu = np.random.random((num_pts,))
            radius = 0.25 * np.cbrt(mu)
            x = radius * np.sin(thetas) * np.cos(phis)
            y = radius * np.sin(thetas) * np.sin(phis)
            z = radius * np.cos(thetas)
            xyz = np.stack((x, y, z), axis=1)

            shs = np.random.random((num_pts, 3)) / 255.0
            C0 = 0.28209479177387814
            color = shs * C0 + 0.5
            pcd = BasicPointCloud(
                points=xyz, colors=color, normals=np.zeros((num_pts, 3))
            )

            self.geometry.create_from_pcd(pcd, 10)
            self.geometry.training_setup()

        self.guidance = threestudio.find(self.cfg.guidance_type)(self.cfg.guidance)
        self.prompt_processor = threestudio.find(self.cfg.prompt_processor_type)(
            self.cfg.prompt_processor
        )
        self.prompt_utils = self.prompt_processor()

        self.edit_frames = {}
        self.edit_patches = {}
        self.edit_json = {}

        if self.cfg.edit_path != "":
            edit_json = dict(
                json.load(open(os.path.join(self.cfg.edit_path, "edit.json"), "r"))
            )
            for key in tqdm(edit_json):
                img = cv2.imread(
                    os.path.join(self.cfg.edit_path, edit_json[key]["file_path"])
                )
                img = torch.FloatTensor(img[:, :, ::-1].copy()).unsqueeze(0) / 255
                self.edit_frames[int(key)] = img
                self.edit_patches[int(key)] = edit_json[key]["patches"]

    def configure_optimizers(self):
        g_optim = self.geometry.gaussian_optimizer
        d_optim = parse_optimizer(self.cfg.optimizer, self)
        dis_optim = parse_optimizer(self.cfg.optimizer.optimizer_dis, self)

        return g_optim, d_optim, dis_optim

    def forward(self, batch: Dict[str, Any]) -> Dict[str, Any]:
        lr_max_step = self.geometry.gaussian.cfg.position_lr_max_steps

        if self.global_step // 2 < lr_max_step:
            self.geometry.gaussian.update_learning_rate(self.global_step // 2)
        else:
            self.geometry.gaussian.update_learning_rate_fine(
                self.global_step // 2 - lr_max_step
            )

        # Every 1000 its we increase the levels of SH up to a maximum degree
        # if (self.gaussians_step) >= self.opt.position_lr_max_steps:
        #     self.gaussians.oneupSHdegree()
        proj = batch["proj"][0]
        fovx, fovy = getFOV(proj, 0.01, 100.0)
        w2c, proj, cam_p = get_cam_info(c2w=batch["c2w"], fovy=fovy, fovx=fovx)

        viewpoint_cam = Camera(
            FoVx=fovx,
            FoVy=fovy,
            image_width=batch["width"],
            image_height=batch["height"],
            world_view_transform=w2c,
            full_proj_transform=proj,
            camera_center=cam_p,
        )

        render_pkg = self.renderer(
            viewpoint_cam,
            batch["moment"][0],
            self.background_tensor,
            patch_x=batch["patch_x"],
            patch_y=batch["patch_y"],
            patch_S=batch["patch_S"],
        )

        return {
            **render_pkg,
        }

    def on_fit_start(self) -> None:
        super().on_fit_start()

    def on_load_checkpoint(self, ckpt_dict) -> None:
        num_pts = ckpt_dict["state_dict"]["geometry.gaussian._xyz"].shape[0]
        pcd = BasicPointCloud(
            points=np.zeros((num_pts, 3)),
            colors=np.zeros((num_pts, 3)),
            normals=np.zeros((num_pts, 3)),
        )
        self.geometry.create_from_pcd(pcd, 10)
        self.geometry.training_setup()
        super().on_load_checkpoint(ckpt_dict)

    def get_patch(self, batch):
        origin_gt_rgb = batch["gt_rgb"]
        B, H, W, C = origin_gt_rgb.shape

        S = self.cfg.get_patch_size
        if batch.__contains__("frame_bbox"):
            bbox = batch["frame_bbox"][0]
            x1, y1, x2, y2 = (
                bbox[0].item(),
                bbox[1].item(),
                bbox[2].item(),
                bbox[3].item(),
            )
            center_x = (x1 + x2) / 2
            center_y = (y1 + y2) / 2
            patch_x = int(min(max(0, center_x - S // 2), W - S - 1))
            patch_y = int(min(max(0, center_y - S // 2), H - S - 1))
        else:
            patch_x = W // 2 - S // 2
            patch_y = H // 2 - S // 2
        batch["patch_x"] = patch_x
        batch["patch_y"] = patch_y
        batch["patch_S"] = S
        return batch

    def training_step(self, batch, batch_idx):
        g_opt, d_opt, dis_opt = self.optimizers()

        if torch.is_tensor(batch["index"]):
            batch_index = batch["index"].item()
        else:
            batch_index = batch["index"]
        origin_gt_rgb = batch["gt_rgb"]
        B, H, W, C = origin_gt_rgb.shape
        batch = self.get_patch(batch)

        S = batch["patch_S"]
        patch_x = batch["patch_x"]
        patch_y = batch["patch_y"]
        if (
            self.cfg.per_editing_step > 0
            and self.global_step >= self.cfg.start_editing_step
        ):
            prompt_utils = self.prompt_processor()
            if (
                not batch_index in self.edit_frames
                or self.global_step % self.cfg.per_editing_step == 0
            ):
                self.renderer.eval()
                full_out = self(batch)
                self.renderer.train()
                refine_size = full_out["refine"].shape[-1]
                if batch.__contains__("frame_mask"):
                    guidance_input = torch.zeros(
                        B, refine_size, refine_size, 4, device=origin_gt_rgb.device
                    )

                    mask = batch["frame_mask"][
                        :, patch_y : patch_y + S, patch_x : patch_x + S
                    ].clone()
                    origin_patch_gt_rgb = origin_gt_rgb[
                        :, patch_y : patch_y + S, patch_x : patch_x + S
                    ]
                    mask = torch.nn.functional.interpolate(
                        mask.permute(0, 3, 1, 2),
                        (refine_size, refine_size),
                        mode="bilinear",
                    ).permute(0, 2, 3, 1)
                    origin_patch_gt_rgb = torch.nn.functional.interpolate(
                        origin_patch_gt_rgb.permute(0, 3, 1, 2),
                        (refine_size, refine_size),
                        mode="bilinear",
                    ).permute(0, 2, 3, 1)

                    guidance_input[:, :, :, :3] = full_out["refine"].unsqueeze(
                        0
                    ).permute(0, 2, 3, 1) * mask[:, :, :, :1] + origin_patch_gt_rgb * (
                        1 - mask[:, :, :, :1]
                    )
                    guidance_input[:, :, :, 3] = 1.0 - mask[:, :, :, 0]
                else:
                    guidance_input = (
                        full_out["refine"]
                        .unsqueeze(0)
                        .permute(0, 2, 3, 1)[
                            :, patch_y : patch_y + S, patch_x : patch_x + S
                        ]
                    )
                result = self.guidance(
                    guidance_input,
                    origin_gt_rgb[:, patch_y : patch_y + S, patch_x : patch_x + S],
                    prompt_utils,
                )
                self.edit_frames[batch_index] = result["edit_images"].detach().cpu()
                self.edit_patches[batch_index] = (patch_x, patch_y)

                save_path = self.get_save_path("edit")
                os.makedirs(save_path, exist_ok=True)
                self.edit_json[batch_index] = {}
                self.edit_json[batch_index]["file_path"] = "%05d.jpg" % batch_index
                self.edit_json[batch_index]["patches"] = [patch_x, patch_y]
                save_img = self.edit_frames[batch_index]
                cv2.imwrite(
                    os.path.join(save_path, "%05d.jpg" % batch_index),
                    (save_img.clamp(0, 1).numpy() * 255).astype(np.uint8)[0][
                        :, :, ::-1
                    ],
                )
                json.dump(
                    self.edit_json, open(os.path.join(save_path, "edit.json"), "w")
                )

        gt_rgb = self.edit_frames[batch_index].to(batch["gt_rgb"].device)
        resize_gt_rgb = torch.nn.functional.interpolate(
            gt_rgb.permute(0, 3, 1, 2), (S, S), mode="bilinear"
        ).permute(0, 2, 3, 1)
        origin_gt_rgb[:, patch_y : patch_y + S, patch_x : patch_x + S] = resize_gt_rgb
        gt_patch_x, gt_patch_y = self.edit_patches[batch_index]

        out = self(batch)

        visibility_filter = out["visibility_filter"]
        radii = out["radii"]
        viewspace_point_tensor = out["viewspace_points"]

        bg_color = out["bg_color"]

        guidance_out = {
            "loss_l1": torch.nn.functional.l1_loss(
                out["render"], origin_gt_rgb.permute(0, 3, 1, 2)[0]
            ),
            "loss_G_l1": torch.nn.functional.l1_loss(
                out["refine"], gt_rgb.permute(0, 3, 1, 2)[0]
            ),
            "loss_G_p": self.perceptual_loss(
                out["refine"].unsqueeze(0).contiguous(),
                gt_rgb.permute(0, 3, 1, 2).contiguous(),
            ).mean(),
            "loss_G_dis": generator_loss(
                self.geometry.discriminator,
                gt_rgb.permute(0, 3, 1, 2),
                out["refine"].unsqueeze(0),
            ),
            "loss_xyz_residual": torch.mean(self.geometry.gaussian._xyz_residual**2),
            "loss_scaling_residual": torch.mean(
                self.geometry.gaussian._scaling_residual**2
            ),
        }
        if out["dynamic_feature_residual"] is not None:
            guidance_out.update(
                {
                    "loss_flow_residual": torch.mean(
                        out["dynamic_feature_residual"]["features"] ** 2
                    ),
                }
            )

        loss = 0.0
        Ll1 = l1_loss(out["render"], origin_gt_rgb.permute(0, 3, 1, 2)[0])
        loss_l1 = (1.0 - 0.2) * Ll1 + 0.2 * (
            1.0 - ssim(out["render"], origin_gt_rgb.permute(0, 3, 1, 2)[0])
        )

        self.log(
            "gauss_num",
            int(self.geometry.get_xyz.shape[0]),
            on_step=True,
            on_epoch=True,
            prog_bar=True,
            logger=True,
        )

        for name, value in guidance_out.items():
            self.log(f"train/{name}", value)
            if name.startswith("loss_") and (not name.startswith("loss_l1")):
                loss += value * self.C(self.cfg.loss[name.replace("loss_", "lambda_")])

        for name, value in self.cfg.loss.items():
            self.log(f"train_params/{name}", self.C(value))

        loss_l1.backward(retain_graph=True)
        iteration = self.global_step // 2
        self.geometry.update_states(
            iteration,
            visibility_filter,
            radii,
            viewspace_point_tensor,
            self.extent,
        )
        loss.backward()
        g_opt.step()
        d_opt.step()
        g_opt.zero_grad(set_to_none=True)
        d_opt.zero_grad(set_to_none=True)

        loss_D = discriminator_loss(
            self.renderer.geometry.discriminator,
            gt_rgb.permute(0, 3, 1, 2),
            out["refine"].unsqueeze(0),
        )
        loss_D *= self.C(self.cfg.loss["lambda_D"])
        self.log("train/loss_D", loss_D)
        loss_D.backward()
        dis_opt.step()
        dis_opt.zero_grad(set_to_none=True)

        return {"loss": loss}

    def validation_step(self, batch, batch_idx):
        batch = self.get_patch(batch)
        out = self(batch)
        if torch.is_tensor(batch["index"]):
            batch_index = batch["index"].item()
        else:
            batch_index = batch["index"]
        if batch_index in self.edit_frames:
            B, H, W, C = batch["gt_rgb"].shape
            rgb = torch.nn.functional.interpolate(
                self.edit_frames[batch_index].permute(0, 3, 1, 2), (H, W)
            ).permute(0, 2, 3, 1)[0]
        else:
            rgb = batch["gt_rgb"][0]
        # import pdb; pdb.set_trace()
        self.save_image_grid(
            f"it{self.global_step}-{batch['index'][0]}.jpg",
            [
                {
                    "type": "rgb",
                    "img": out["render"].unsqueeze(0).permute(0, 2, 3, 1)[0],
                    "kwargs": {"data_format": "HWC"},
                },
            ]
            + [
                {
                    "type": "rgb",
                    "img": out["refine"].unsqueeze(0).permute(0, 2, 3, 1)[0],
                    "kwargs": {"data_format": "HWC"},
                },
            ]
            + [
                {
                    "type": "rgb",
                    "img": rgb,
                    "kwargs": {"data_format": "HWC", "data_range": (0, 1)},
                },
            ],
            name="validation_step",
            step=self.global_step,
        )

    def on_validation_epoch_end(self):
        pass

    def test_step(self, batch, batch_idx):
        batch = self.get_patch(batch)
        out = self(batch)
        self.save_image_grid(
            f"it{self.global_step}-test/{batch['index'][0]}.jpg",
            [
                {
                    "type": "rgb",
                    "img": out["render"].unsqueeze(0).permute(0, 2, 3, 1)[0],
                    "kwargs": {"data_format": "HWC"},
                },
            ]
            + [
                {
                    "type": "rgb",
                    "img": out["refine"].unsqueeze(0).permute(0, 2, 3, 1)[0],
                    "kwargs": {"data_format": "HWC"},
                },
            ],
            name="test_step",
            step=self.global_step,
        )

    def on_test_epoch_end(self):
        self.save_img_sequence(
            f"it{self.global_step}-test",
            f"it{self.global_step}-test",
            "(\d+)\.jpg",
            save_format="mp4",
            fps=30,
            name="test",
            step=self.global_step,
        )
