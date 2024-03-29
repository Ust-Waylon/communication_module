import argparse
import time
import datetime
import inspect
import os
import re
import gradio as gr
from omegaconf import OmegaConf

import torch

import diffusers
from diffusers import AutoencoderKL, DDIMScheduler

from tqdm.auto import tqdm
from transformers import CLIPTextModel, CLIPTokenizer

from AnimateDiff.animatediff.models.unet import UNet3DConditionModel
from AnimateDiff.animatediff.pipelines.pipeline_animation import AnimationFreeInitPipeline
from AnimateDiff.animatediff.utils.util import save_videos_grid
from AnimateDiff.animatediff.utils.util import load_weights
from diffusers.utils.import_utils import is_xformers_available

from einops import rearrange, repeat

import csv, pdb, glob
import math
from pathlib import Path
from diffusers.training_utils import set_seed

class AnimateDiffPipeline:
    def __init__(self):
        self.pretrained_model_path = "/project/t3_wtanae/AnimateDiff/models/StableDiffusion/stable-diffusion-v1-5"
        self.inference_config = "AnimateDiff/configs/inference/inference-v2.yaml"
        self.config = "AnimateDiff/configs/prompts/RealisticVision_v2.yaml"

        # 4：3
        self.W = 512
        self.H = 384

        # 16：9
        # self.W = 1024
        # self.H = 576
        
        self.L = 16

        self.num_samples = 10

        self.model_config  = OmegaConf.load(self.config)

        set_seed(42)

        self.motion_module = self.model_config.motion_module[0]

        self.inference_config = OmegaConf.load(self.inference_config)

        ### >>> create validation pipeline >>> ###
        self.tokenizer    = CLIPTokenizer.from_pretrained(self.pretrained_model_path, subfolder="tokenizer")
        self.text_encoder = CLIPTextModel.from_pretrained(self.pretrained_model_path, subfolder="text_encoder")
        self.vae          = AutoencoderKL.from_pretrained(self.pretrained_model_path, subfolder="vae")
        self.unet         = UNet3DConditionModel.from_pretrained_2d(self.pretrained_model_path, subfolder="unet", unet_additional_kwargs=OmegaConf.to_container(self.inference_config.unet_additional_kwargs))

        if is_xformers_available(): self.unet.enable_xformers_memory_efficient_attention()
        else: assert False

        self.pipeline = AnimationFreeInitPipeline(
            vae=self.vae, text_encoder=self.text_encoder, tokenizer=self.tokenizer, unet=self.unet,
            scheduler=DDIMScheduler(**OmegaConf.to_container(self.inference_config.noise_scheduler_kwargs)),
        ).to("cuda")

        self.pipeline = load_weights(
            self.pipeline,
            # motion module
            motion_module_path         = self.motion_module,
            motion_module_lora_configs = self.model_config.get("motion_module_lora_configs", []),
            # image layers
            dreambooth_model_path      = self.model_config.get("dreambooth_path", ""),
            lora_model_path            = self.model_config.get("lora_model_path", ""),
            lora_alpha                 = self.model_config.get("lora_alpha", 0.8),
        ).to("cuda")

        # (freeinit) initialize frequency filter for noise reinitialization -------------
        self.pipeline.init_filter(
            width               = self.W,
            height              = self.H,
            video_length        = self.L,
            filter_params       = self.model_config.filter_params,
        )

        self.savedir = ""
        self.save_prompt = ""

        self.general_positive_prompt = "best quality, masterpiece, extremely detailed, highres, 8k"
        self.negative_prompt = "(worst quality:2), (low quality:2), (normal quality:2), (deformed iris, deformed pupils, semi-realistic, cgi, 3d, render, sketch, cartoon, drawing, anime, mutated hands and fingers:1.4), (deformed, distorted, disfigured:1.3), poorly drawn, bad anatomy, wrong anatomy, extra limb, missing limb, floating limbs, disconnected limbs, mutation, mutated, ugly, disgusting, amputation, naked human body"

    def generate_video(self, prompt, n_prompt, num_samples):
        time_str = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        if not os.path.exists("outputs"):
            os.makedirs("outputs")
        savedir = f"outputs/AnimateDiff_{time_str}"
        os.makedirs(savedir)

        prompt = f"{prompt}, {self.general_positive_prompt}"
        print(f"sampling {prompt}")
        save_prompt = "-".join((prompt.replace("/", "").split(" ")[:10]))

        self.savedir = savedir
        self.save_prompt = save_prompt

        for sample_idx in range(num_samples):
            sample = self.pipeline(
                        prompt,
                        negative_prompt     = n_prompt,
                        num_inference_steps = self.model_config.steps,
                        guidance_scale      = self.model_config.guidance_scale,
                        width               = self.W,
                        height              = self.H,
                        video_length        = self.L,
                        num_iters = 1,
                        use_fast_sampling = False,
                        save_intermediate = False,
                        save_dir = f"{savedir}/sample/intermediate",
                        save_name = f"{0}-{save_prompt}",
                        use_fp16            = True
                    ).videos
            
            save_videos_grid(sample, f"{savedir}/{sample_idx}-{save_prompt}.mp4")
            print(f"save to {savedir}/{sample_idx}-{save_prompt}.mp4")

        return savedir, save_prompt
    
    def generate_video_for_app(self, textbox, progress = gr.Progress()):
        prompt = textbox
        print("video prompt: ", prompt)
        n_prompt = self.negative_prompt
        num_samples = self.num_samples
        savedir, save_prompt = self.generate_video(prompt, n_prompt, num_samples)
    
    def switch_show_video(self, show_video_id):
        print("switch showing video: ", show_video_id)
        return gr.Video(label="Generated video", value=f"{self.savedir}/{show_video_id-1}-{self.save_prompt}.mp4", visible=True)
    
    def check_generation_progress(self):
        for i in range(self.num_samples):
            if not os.path.exists(f"{self.savedir}/{i}-{self.save_prompt}.mp4"):
                break
        return i
    
    def track_generation_progress(self, progress=gr.Progress()):
        # wait for savedir update
        time.sleep(3)
        # track generation progress
        i = 0
        progress_step = 1 / self.num_samples
        progress(0, f"generating the 1st sample")
        while self.check_generation_progress() < self.num_samples - 1:
            i = self.check_generation_progress()
            progress(progress_step * i, f"generating the {i + 1}th sample")
            time.sleep(1)
        return gr.Video(label="Generated video", value=f"{self.savedir}/0-{self.save_prompt}.mp4", visible=True)

    def restart(self):
        self.savedir = ""
        self.save_prompt = ""
        print("AnimateDiff pipeline restarted")
        
        

if __name__ == "__main__":
    animatediff_pipeline = AnimateDiffPipeline()

    num_samples = 5

    while True:
        prompt = input("Enter prompt: ")
        n_prompt = "(deformed iris, deformed pupils, semi-realistic, cgi, 3d, render, sketch, cartoon, drawing, anime, mutated hands and fingers:1.4), (deformed, distorted, disfigured:1.3), poorly drawn, bad anatomy, wrong anatomy, extra limb, missing limb, floating limbs, disconnected limbs, mutation, mutated, ugly, disgusting, amputation"
        savedir, save_prompt = animatediff_pipeline.generate_video(prompt, n_prompt, num_samples)
