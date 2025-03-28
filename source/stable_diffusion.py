# Peekaboo: Text to Image Diffusion Models Are Zero-Shot Segmentors
#
# Copyright (c) 2023 Ryan Burgert
#
# This code is based on the Stable-Dreamfusion codebase's 'sd.py' by Jiaxiang Tang (https://github.com/ashawkey/stable-dreamfusion)
# which is licensed under the Apache License Version 2.0.
# It has been heavily modified to suit Peekaboo's needs, but the basic concepts remain the same.
# Tensor shape assertions have been added to the code to make it easier to read.
#
# Author: Ryan Burgert

from typing import Union,List,Optional

from transformers import CLIPTextModel, CLIPTokenizer, logging
from diffusers import AutoencoderKL, UNet2DConditionModel, PNDMScheduler
from diffusers import StableDiffusionPipeline

import torch
import torch.nn as nn
import torch.nn.functional as F

import rp

# Suppress partial model loading warning
logging.set_verbosity_error()

class StableDiffusion(nn.Module):
    def __init__(self, device='cuda', checkpoint_path="CompVis/stable-diffusion-v1-4"):
        super().__init__()

        self.device = torch.device(device)
        self.num_train_timesteps = 1000
        
        # Timestep ~ U(0.02, 0.98) to avoid very high/low noise levels
        self.min_step = int(self.num_train_timesteps * 0.02) # aka 20
        self.max_step = int(self.num_train_timesteps * 0.98) # aka 980

        print('[INFO] sd.py: loading stable diffusion...please make sure you have run `huggingface-cli login`.')
        
        # Unlike the original code, I'll load these from the pipeline. This lets us use dreambooth models.
        pipe = StableDiffusionPipeline.from_pretrained(checkpoint_path, torch_dtype=torch.float)
        pipe.safety_checker = lambda images, _: images, False # Disable the NSFW checker (slows things down)
    
        pipe.scheduler = PNDMScheduler(beta_start=0.00085, beta_end=0.012, beta_schedule="scaled_linear", num_train_timesteps=self.num_train_timesteps) #Error from scheduling_lms_discrete.py
        
        self.pipe         = pipe
        self.vae          = pipe.vae         .to(self.device) ; assert isinstance(self.vae          , AutoencoderKL       ),type(self.vae          )
        self.tokenizer    = pipe.tokenizer                    ; assert isinstance(self.tokenizer    , CLIPTokenizer       ),type(self.tokenizer    )
        self.text_encoder = pipe.text_encoder.to(self.device) ; assert isinstance(self.text_encoder , CLIPTextModel       ),type(self.text_encoder )
        self.unet         = pipe.unet        .to(self.device) ; assert isinstance(self.unet         , UNet2DConditionModel),type(self.unet         )
        self.scheduler    = pipe.scheduler                    ; #assert isinstance(self.scheduler    , PNDMScheduler       ),type(self.scheduler    )
        
        self.uncond_text=''

        self.checkpoint_path=checkpoint_path
            
        self.alphas = self.scheduler.alphas_cumprod.to(self.device) # for convenience

        print(f'[INFO] sd.py: loaded stable diffusion!')

    def get_text_embeddings(self, prompts: Union[str, List[str]])->torch.Tensor:
        
        if isinstance(prompts,str):
            prompts=[prompts]

        # Tokenize text and get embeddings
        text_input = self.tokenizer(prompts, padding='max_length', max_length=self.tokenizer.model_max_length, truncation=True, return_tensors='pt').input_ids

        with torch.no_grad():
            text_embeddings = self.text_encoder(text_input.to(self.device))[0]

        # Do the same for unconditional embeddings
        uncond_input = self.tokenizer([self.uncond_text] * len(prompts), padding='max_length', max_length=self.tokenizer.model_max_length, return_tensors='pt').input_ids

        with torch.no_grad():
            uncond_embeddings = self.text_encoder(uncond_input.to(self.device))[0]

        assert len(uncond_embeddings)==len(text_embeddings)==len(prompts)==len(text_input)==len(uncond_input)

        output_embeddings = torch.cat([uncond_embeddings, text_embeddings])

        assert (uncond_embeddings==torch.stack([uncond_embeddings[0]]*len(uncond_embeddings))).all()
        assert (uncond_embeddings==uncond_embeddings[0][None]).all()

        assert output_embeddings.shape == (len(prompts)*2, 77, 768)

        return output_embeddings

    def train_step(self, text_embeddings:torch.Tensor, pred_rgb:torch.Tensor, guidance_scale:float=100, t:Optional[int]=None):
        
        # This method is responsible for generating the dream-loss gradients.
        
        # interp to 512x512 to be fed into vae
        pred_rgb_512 = F.interpolate(pred_rgb, (512, 512), mode='bilinear', align_corners=False)

        if t is None:
            t = torch.randint(self.min_step, self.max_step + 1, [1], dtype=torch.long, device=self.device)

        assert 0<=t<self.num_train_timesteps, 'invalid timestep t=%i'%t

        # encode image into latents with vae, requires grad!
        latents = self.encode_imgs(pred_rgb_512)

        # predict the noise residual with unet, NO grad!
        with torch.no_grad():
            # add noise
            noise = torch.randn_like(latents)
            latents_noisy = self.scheduler.add_noise(latents, noise, t.cpu())
            # pred noise
            latent_model_input = torch.cat([latents_noisy] * 2)
            noise_pred = self.unet(latent_model_input, t, encoder_hidden_states=text_embeddings)['sample']

        # perform guidance (high scale from paper!)
        noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
        noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_text - noise_pred_uncond)

        # w(t), sigma_t^2
        w = (1 - self.alphas[t])
        grad = w * (noise_pred - noise)

        # manually backward, since we omitted an item in grad and cannot simply autodiff
        latents.backward(gradient=grad, retain_graph=True)
        return 0 # dummy loss value

    def produce_latents(self, text_embeddings:torch.Tensor, height:int=512, width:int=512, num_inference_steps=50, guidance_scale=7.5, latents=None)->torch.Tensor:
        assert len(text_embeddings.shape)==3 and text_embeddings.shape[-2:]==(77,768)
        assert not len(text_embeddings)%2
        num_prompts = len(text_embeddings)//2

        if latents is None:
            latents = torch.randn((num_prompts, self.unet.in_channels, height // 8, width // 8), device=self.device)

        assert 0 <= num_inference_steps <= 1000, 'Stable diffusion appears to be trained with 1000 timesteps'

        self.scheduler.set_timesteps(num_inference_steps)

        with torch.autocast('cuda'):
            for i, t in enumerate(self.scheduler.timesteps):
                assert int(t) == t and 0 <= t <= 999, 'Suprisingly to me...the timesteps were encoded as integers lol (np.int64)'
                assert int(i) == i and 0 <= i <= 999, 'And because there are 1000 of them, the index is also bounded'
                t=int(t) # This akes some schedulers happy; it's the same value anyway.

                # Expand the latents if we are doing classifier-free guidance to avoid doing two forward passes.
                latent_model_input = torch.cat([latents] * 2) #The first half is the blank prompts (repeated); the second half is 

                # predict the noise r['sample']
                with torch.no_grad():
                    noise_pred = self.unet(latent_model_input, t, encoder_hidden_states=text_embeddings)['sample']
                    assert len(latent_model_input)==len(text_embeddings)==len(noise_pred)

                # perform guidance
                assert noise_pred.shape == (2*num_prompts, 4, 64, 64)
                noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
                noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_text - noise_pred_uncond)
                assert noise_pred.shape == (1*num_prompts, 4, 64, 64)

                # compute the previous noisy sample x_t -> x_t-1
                latents = self.scheduler.step(noise_pred, t, latents)['prev_sample'] #It's a dict with nothing but 'prev_sample' in it
                assert latents.shape==noise_pred.shape == (num_prompts, 4, 64, 64)

        return latents

    def decode_latents(self, latents:torch.Tensor)->torch.Tensor:

        assert len(latents.shape) == 4 and latents.shape[1] == 4  # [B, 4, H, W]
        
        latents = 1 / 0.18215 * latents
        
        imgs = self.vae.decode(latents)
        if hasattr(imgs,'sample'):
            #For newer versions of the Diffusers library
            imgs=imgs.sample

        imgs = (imgs / 2 + 0.5).clamp(0, 1)
        
        assert len(imgs.shape) == 4 and imgs.shape[1] == 3  # [B, 3, H, W]
        
        return imgs

    # def encode_imgs(self, imgs:torch.Tensor)->torch.Tensor:
        
    #     assert len(imgs.shape)==4 and imgs.shape[1]==3 #[B, 3, H, W]

    #     imgs = 2 * imgs - 1
    #     posterior = self.vae.encode(imgs)
    #     latents = posterior.sample() * 0.18215
        
    #     assert len(latents.shape)==4 and latents.shape[1]==4 #[B, 4, H, W]

    #     return latents
    def encode_imgs(self, imgs: torch.Tensor) -> torch.Tensor:
      assert len(imgs.shape) == 4 and imgs.shape[1] == 3  # [B, 3, H, W]

      imgs = 2 * imgs - 1
      posterior = self.vae.encode(imgs)

      # Sửa lỗi: lấy mẫu từ latent_dist thay vì gọi trực tiếp trên posterior
      latents = posterior.latent_dist.sample() * 0.18215

      assert len(latents.shape) == 4 and latents.shape[1] == 4  # [B, 4, H, W]

      return latents


    def decode_latent(self, latent: torch.Tensor) -> torch.Tensor:

        assert len(latent.shape) == 3 and latent.shape[0] == 4  # [4, H, W]

        img = self.decode_latents(latent[None])[0]

        assert len(img.shape) == 3 and img.shape[0] == 3  # [3, H, W]

        return img

    def encode_img(self, img: torch.Tensor) -> torch.Tensor:

        assert len(img.shape) == 3 and img.shape[0] == 3  # [3, H, W]

        latent = self.encode_imgs(img[None])[0]

        assert len(latent.shape) == 3 and latent.shape[0] == 4  # [4, H, W]

        return latent
    
    def embeddings_to_imgs(self, text_embeddings:torch.Tensor, 
                     height:int=512, 
                     width:int=512,
                     num_inference_steps:int=50,
                     guidance_scale:float=7.5, 
                     latents:Optional[torch.Tensor]=None)->torch.Tensor:
        
        assert len(text_embeddings.shape)==3 and text_embeddings.shape[1:]==(77,768)
        assert not len(text_embeddings)%2
        num_prompts=len(text_embeddings)//2

        # text embeddings -> img latents
        latents = self.produce_latents(text_embeddings, 
                                       height=height, 
                                       width=width, 
                                       latents=latents, 
                                       num_inference_steps=num_inference_steps,
                                       guidance_scale=guidance_scale)
        assert latents.shape==(num_prompts, 4, 64, 64)
        
        # img latents -> imgs
        with torch.no_grad:
            imgs = self.decode_latents(latents) 
        assert imgs.shape==(num_prompts,3,512,512)

        # torch imgs -> numpy imgs
        imgs = rp.as_numpy_images(imgs)
        assert imgs.shape==(num_prompts,512,512,3)
 
        return imgs
    
    def prompts_to_imgs(self, prompts: List[str], 
                        height:int=512, 
                        width:int=512, 
                        num_inference_steps:int=50, 
                        guidance_scale:float=7.5, 
                        latents:Optional[torch.Tensor]=None)->torch.Tensor:

        if isinstance(prompts, str):
            prompts = [prompts]

        # prompts -> text embeddings
        text_embeddings = self.get_text_embeddings(prompts)
        assert text_embeddings.shape==( len(prompts)*2, 77, 768 )
        
        return self.embeddings_to_imgs(text_embeddings, height, width, num_inference_steps, guidance_scale, latents)
    
    def prompt_to_img(self, prompt:str, *args, **kwargs)->torch.Tensor:
        return self.prompts_to_imgs([prompt],*args,**kwargs)[0]

