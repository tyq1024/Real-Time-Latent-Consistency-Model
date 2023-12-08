from diffusers import (
    StableDiffusionControlNetImg2ImgPipeline,
    AutoencoderTiny,
    ControlNetModel,
    UNet2DConditionModel,
)
from compel import Compel
import torch
from pipelines.utils.canny_gpu import SobelOperator
from pipeline_latent_consistency_img2img_controlnet import LatentConsistencyModelImg2ImgPipelineControlnet

try:
    import intel_extension_for_pytorch as ipex  # type: ignore
except:
    pass

import psutil
from config import Args
from pydantic import BaseModel, Field
from PIL import Image
import time

base_model = "SimianLuo/LCM_Dreamshaper_v7"
unet_model = "SimianLuo/LCM_Dreamshaper_v7"
unet = UNet2DConditionModel.from_pretrained(
    unet_model, subfolder="unet", low_cpu_mem_usage=False, local_files_only=True
)
taesd_model = "madebyollin/taesd"
controlnet_model = "lllyasviel/control_v11p_sd15_canny"
controlnet_model_hed = "lllyasviel/sd-controlnet-hed"
#default_prompt = "Portrait of The Terminator with , glare pose, detailed, intricate, full of colour, cinematic lighting, trending on artstation, 8k, hyperrealistic, focused, extreme details, unreal engine 5 cinematic, masterpiece"
default_prompt = "Portrait of Joker Halloween costume, face painting, glare pose, detailed, intricate, full of colour, cinematic lighting, trending on artstation, 8k, hyperrealistic, focused, extreme details, unreal engine 5 cinematic, masterpiece"


class Pipeline:
    class Info(BaseModel):
        name: str = "controlnet"
        title: str = "LCM + Controlnet"
        description: str = "Generates an image from a text prompt"
        input_mode: str = "image"

    class InputParams(BaseModel):
        prompt: str = Field(
            default_prompt,
            title="Prompt",
            field="textarea",
            id="prompt",
        )
        seed: int = Field(
            2159232, min=0, title="Seed", field="seed", hide=True, id="seed"
        )
        steps: int = Field(
            1, min=1, max=15, title="Steps", field="range", hide=True, id="steps"
        )
        width: int = Field(
            512, min=2, max=15, title="Width", disabled=True, hide=True, id="width"
        )
        height: int = Field(
            512, min=2, max=15, title="Height", disabled=True, hide=True, id="height"
        )
        guidance_scale: float = Field(
            1.0,
            min=0,
            max=5,
            step=0.001,
            title="Guidance Scale",
            field="range",
            hide=True,
            id="guidance_scale",
        )
        strength: float = Field(
            0.5,
            min=0.25,
            max=1.0,
            step=0.001,
            title="Strength",
            field="range",
            hide=True,
            id="strength",
        )
        controlnet_scale: float = Field(
            0.8,
            min=0,
            max=1.0,
            step=0.001,
            title="Controlnet Scale",
            field="range",
            hide=True,
            id="controlnet_scale",
        )
        controlnet_start: float = Field(
            0.0,
            min=0,
            max=1.0,
            step=0.001,
            title="Controlnet Start",
            field="range",
            hide=True,
            id="controlnet_start",
        )
        controlnet_end: float = Field(
            1.0,
            min=0,
            max=1.0,
            step=0.001,
            title="Controlnet End",
            field="range",
            hide=True,
            id="controlnet_end",
        )
        canny_low_threshold: float = Field(
            0.31,
            min=0.01,
            max=0.99,
            step=0.001,
            title="Canny Low Threshold",
            field="range",
            hide=True,
            id="canny_low_threshold",
        )
        canny_high_threshold: float = Field(
            0.125,
            min=0.01,
            max=0.99,
            step=0.001,
            title="Canny High Threshold",
            field="range",
            hide=True,
            id="canny_high_threshold",
        )
        debug_canny: bool = Field(
            False,
            title="Debug Canny",
            field="checkbox",
            hide=True,
            id="debug_canny",
        )

    def __init__(self, args: Args, device: torch.device, torch_dtype: torch.dtype):
        controlnet_canny = ControlNetModel.from_pretrained(
            controlnet_model, torch_dtype=torch_dtype
        ).to(device)
        controlnet_hed = ControlNetModel.from_pretrained(
            controlnet_model_hed, torch_dtype=torch_dtype
        ).to(device)        
        controlnets = [controlnet_canny, controlnet_hed]
        if args.safety_checker:
            self.pipe = LatentConsistencyModelImg2ImgPipelineControlnet.from_pretrained(
                base_model, controlnet=controlnets, unet=unet, device=device
            )
        else:
            self.pipe = LatentConsistencyModelImg2ImgPipelineControlnet.from_pretrained(
                base_model,
                safety_checker=None,
                controlnet=controlnets,
                unet=unet,
                device=device
            )
        if args.use_taesd:
            self.pipe.vae = AutoencoderTiny.from_pretrained(
                taesd_model, torch_dtype=torch_dtype, use_safetensors=True
            )
        self.canny_torch = SobelOperator(device=device)
        self.pipe.set_progress_bar_config(disable=True)
        self.pipe.to(device=device, dtype=torch_dtype)
        if device.type != "mps":
            self.pipe.unet.to(memory_format=torch.channels_last)

        # check if computer has less than 64GB of RAM using sys or os
        if psutil.virtual_memory().total < 64 * 1024**3:
            self.pipe.enable_attention_slicing()

        if args.torch_compile:
            if torch.cuda.is_available():
                self.pipe.unet.to(memory_format=torch.channels_last)
                self.pipe.vae.to(memory_format=torch.channels_last)
                if hasattr(self.pipe, "controlnet"):
                    self.pipe.controlnet.to(memory_format=torch.channels_last)
            self.pipe.unet = torch.compile(
                self.pipe.unet, mode="max-autotune", fullgraph=True
            )
            self.pipe.vae = torch.compile(
                self.pipe.vae, mode="max-autotune", fullgraph=True
            )
            # if hasattr(self.pipe, "controlnet"):
            #     self.pipe.controlnet = torch.compile(
            #         self.pipe.controlnet, mode="max-autotune", fullgraph=True
            #     )

            self.pipe(
                prompt="warmup",
                image=[Image.new("RGB", (512, 512))],
                control_image=[Image.new("RGB", (512, 512)) for _ in range(2)],
                controlnet_conditioning_scale=[0., 0.],
            )

        if args.use_sfast:
            from sfast.compilers.stable_diffusion_pipeline_compiler import (
                compile, CompilationConfig)

            sfast_config = CompilationConfig.Default()
            try:
                import xformers
                sfast_config.enable_xformers = True
            except ImportError:
                print('xformers not installed, skip')
            try:
                import triton
                sfast_config.enable_triton = True
            except ImportError:
                print('Triton not installed, skip')
            sfast_config.enable_cuda_graph = True
            sfast_config.preserve_parameters = False
            self.pipe = compile(self.pipe, sfast_config)

        self.compel_proc = Compel(
            tokenizer=self.pipe.tokenizer,
            text_encoder=self.pipe.text_encoder,
            truncate_long_prompts=False,
        )

        self.store_prompt_embeds = None

        self.last_image = Image.new("RGB", (512, 512))

    def predict(self, params: "Pipeline.InputParams") -> Image.Image:
        generator = torch.manual_seed(params.seed)
        if self.store_prompt_embeds is None:
            prompt_embeds = self.compel_proc(params.prompt)
            self.store_prompt_embeds = prompt_embeds
        else:
            prompt_embeds = self.store_prompt_embeds
        input_image = params.image.resize((512, 512))
        control_image_canny = self.canny_torch(
            input_image, params.canny_low_threshold, params.canny_high_threshold
        )

        results = self.pipe(
            image=input_image,
            control_image=[control_image_canny, control_image_canny],
            prompt_embeds=prompt_embeds,
            generator=generator,
            strength=params.strength,
            num_inference_steps=params.steps,
            guidance_scale=params.guidance_scale,
            width=params.width,
            height=params.height,
            output_type="pil",
            controlnet_conditioning_scale=[params.controlnet_scale, params.controlnet_scale],
            control_guidance_start=params.controlnet_start,
            control_guidance_end=params.controlnet_end,
        )

        nsfw_content_detected = (
            results.nsfw_content_detected[0]
            if "nsfw_content_detected" in results
            else False
        )
        if nsfw_content_detected:
            return None
        result_image = results.images[0]
        if params.debug_canny:
            # paste control_image on top of result_image
            w0, h0 = (200, 200)
            control_image_canny = control_image_canny.resize((w0, h0))
            w1, h1 = result_image.size
            result_image.paste(control_image_canny, (w1 - w0, h1 - h0))

        return result_image
