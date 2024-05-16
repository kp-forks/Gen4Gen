import os
import re
import cv2
import PIL
import glob
import torch
import argparse
import numpy as np
import pandas as pd
import os.path as osp
import torchvision.transforms.functional as F

from tqdm import tqdm
from pathlib import Path
from rich.progress import track
from PIL import Image, ImageFilter
from collections import defaultdict
from diffusers import (
        StableDiffusionInpaintPipeline, AutoPipelineForInpainting,
        AutoPipelineForText2Image,
        StableDiffusionPipeline, StableDiffusionUpscalePipeline)

from rich.console import Console
from rich.markdown import Markdown

import inflect
from skimage import exposure
from typing import List

VALID_IMAGE_EXTENSION: tuple = (
    'rgb', 'gif', 'pbm', 'pgm', 'ppm', 'tiff', 'rast',
    'xbm', 'jpeg', 'jpg', 'bmp', 'png', 'webp', 'ext')

CAPTION = "a realistic photo of {prompt}"

NEG_PROMPT = "semi-realistic, cgi, 3d, render, sketch, cartoon, drawing, anime, text, close up, cropped, out of frame, worst quality, low quality, jpeg artifacts, ugly, duplicate, morbid, mutilated, extra fingers, mutated hands, poorly drawn hands, poorly drawn face, mutation, deformed, blurry, dehydrated, bad anatomy, bad proportions, extra limbs, cloned face, disfigured, gross proportions, malformed limbs, missing arms, missing legs, extra arms, extra legs, fused fingers, too many fingers, long neck, mixture"

def parse_args():
    parser = argparse.ArgumentParser(description='Saliency Object Detection')
    parser.add_argument('--src-dir', default='', help='source directory')
    parser.add_argument('--bkg-dir', default='', nargs='+', help='background directory')
    parser.add_argument('--dest', default='gen_samples',
            help='saved directory (path for generated images)')
    parser.add_argument('--ann-dir', default='gen_annotations',
            help='saved directory (annotations of generated images)')
    parser.add_argument('-gs', '--guidance-scale', type=float, default=6.0)
    parser.add_argument('--num-steps', type=int, default=60)
    parser.add_argument('--strength', type=float, default=0.9)
    parser.add_argument('--inpaint-model-name', type=str,
        default="sd-xl-1.0",
        choices=["sd-1.5", "sd-xl-1.0"],
        help='inpainting diffusion models')
    parser.add_argument('--prompt', type=str, default='', help='')
    parser.add_argument('--resolution', type=int, default=1024)
    parser.add_argument('--batch-size', type=int, default=1)
    parser.add_argument('--blur-size', type=int, default=5)
    parser.add_argument(
        "--objects",
        type=str,
        nargs="+",
        default=None,
        help="manual designed objects"
    )
    parser.add_argument('-n-bkg', '--max-num_bkg-scenes', type=int, default=2)
    parser.add_argument('--noise-bkg', action='store_true')
    args = parser.parse_args()
    return args

# Inpainting setting
sd_inpaint_dict = {
    "sd-1.5": dict(
        sd_pipe=StableDiffusionInpaintPipeline,
        model="runwayml/stable-diffusion-inpainting"),
    "sd-xl-1.0": dict(
        sd_pipe=AutoPipelineForInpainting,
        model="diffusers/stable-diffusion-xl-1.0-inpainting-0.1",
        refiner="stabilityai/stable-diffusion-xl-refiner-1.0"),
}

# Create annotation file in csv format
OUT_ANN_HEADER: list = ['directory', 'image_name', 'background_prompt']

# NOTE: hard-code prompt (we copy the background description generated by LLM in step 2)
# in this dictionary, `key` is background folder name, `value` is the background prompt generated by LLM model
bkg_to_prompt: dict = {
    'garden': 'in a garden',
    'garden_lake': 'in a garden with the lake',
    'room': 'in a room',
    'farm': 'in the farm',
    'grass': 'on the grass',
    'galaxy': 'in the galaxy',
    'office': 'in the office',
    'forest': 'in the forest',
    'beach': 'on the beach',
    'museum': 'in the museum',
    'nationalpark': 'in the national park',
    'sand': 'on the sand',
    'sky': 'in the sky',
    'park': 'at the park',
    'airport': 'at the airport',
    'mountain': 'over the mountain',
    'sea': 'on the sea',
    'harbor': 'in the harbor',
    'farm': 'on the farm',
    'field': 'in the field',
    'countryside': 'in the countryside',
    'bedroom': 'in the bedroom',
    'living-room': 'in the living room',
    'playroom': 'in the playroom',
    'kitchen': 'in the kitchen',
    'savannah': 'on the savannah',
    'zoo': 'at the zoo',
    'safari': 'in the safari',
    'circus': 'at the circus',
    'pasture': 'at the pasture',
    'meadow': 'on the meadow',
    'ocean': 'in the ocean',
    'coral-reef': 'on the coral reef',
    'aquarium': 'in the aquarium',
    'underwater-cave': 'in the underwater cave',
    'medieval-village': 'in the medieval village',
    'sidewalk': 'on the sidewalk',
    'playground': 'at the playground',
    'clothing-store': 'in the clothing store',
    'closet': 'in the closet',
    'laboratory': 'in the laboratory',
    'jungle': 'in the jungle',
    'city': 'in the city',
    'desert': 'in the desert',
    'oasis': 'in the oasis',
    'pond': 'in the pond',
    'gym': 'in the gym',
    'grasslands': 'on the grasslands',
    'gallery': 'in the gallery',
    'cafe': 'in the cafe',
}

def main():
    args = parse_args()

    # Output image resolution (default: 1024 by SD-XL Inpainting)
    H, W = args.resolution, args.resolution

    console = Console()
    num_to_words_engine = inflect.engine() # turn number to words

    out_dir_name = f"{Path(args.src_dir).stem}_repaint"
    args.dest: str = osp.join(args.dest, out_dir_name)
    Path(args.dest).mkdir(parents=True, exist_ok=True)

    args.ann_dir: str = osp.join(args.dest, args.ann_dir)
    Path(args.ann_dir).mkdir(parents=True, exist_ok=True)

    out_ann_name = f"{out_dir_name}_ann.csv"
    if osp.isfile(osp.join(args.ann_dir, out_ann_name)):
        print('Remove exiting output annotations, will create a new one')
        os.system(f'rm -v {osp.join(args.ann_dir, out_ann_name)}')
    with open(osp.join(args.ann_dir, out_ann_name), 'a+') as fp:
        fp.write(','.join(OUT_ANN_HEADER) + '\n')

    # Get stable diffusion model name and pipe for diffuser
    sd_pipe: str = sd_inpaint_dict[args.inpaint_model_name]['sd_pipe']
    model_name: str = sd_inpaint_dict[args.inpaint_model_name]['model']

    # Get composed images (only foreground objects)
    image_ids = np.array([f for f in os.listdir(args.src_dir) if 'img' in f])

    # Get background images
    bkg_dict = defaultdict(list)

    np.random.seed(0)
    for bkg_dir in args.bkg_dir:
        for root, dirs, files in os.walk(bkg_dir, topdown=False):
            files = sorted([f for f in files if f.lower().endswith(VALID_IMAGE_EXTENSION)])
            if not bool(files): continue # skip empty folders

            files = [osp.join(root, f) for f in files]
            files = np.random.choice(files, args.max_num_bkg_scenes, replace=False)
            bkg_dict[Path(root).stem] = files.tolist()

    # Initialize diffuser pipe
    pipe = sd_pipe.from_pretrained(
        model_name,
        torch_dtype=torch.float16,
        variant="fp16").to('cuda')

    with_refiner = True if 'refiner' in sd_inpaint_dict[args.inpaint_model_name] else False
    if with_refiner:
        refiner = sd_pipe.from_pretrained(
                sd_inpaint_dict[args.inpaint_model_name]['refiner'],
                text_encoder_2=pipe.text_encoder_2,
                vae=pipe.vae,
                torch_dtype=torch.float16,
                use_safetensors=True,
                variant="fp16",
            ).to("cuda")

    print()
    console.rule("[bold blue]3️⃣  Step 3: Background Repainting")

    for idx, img_id in enumerate(image_ids):

        img_path: str = osp.join(args.src_dir, img_id)
        mask_path: str = img_path.replace('img', 'mask') # get mask image path

        cnt = 0
        for bkg_dir, bkg_files in bkg_dict.items():
            for bkg_file in bkg_files:
                print(f"Load the background file: {bkg_file}")

                if args.noise_bkg: # Using the noise background
                    noise = np.random.randint(low=0, high=255, size=(H, W, 3)) # [low, high)
                    bkg: PIL.Image = Image.fromarray(noise.astype(np.uint8)).convert("RGB") # H x W x 3
                else: # Load downloaded background
                    bkg = Image.open(bkg_file).convert("RGB") # H x W x 3
                    bkg: PIL.Image = bkg.filter(ImageFilter.GaussianBlur(radius=1))

                # load image and mask every time
                img = Image.open(img_path).convert("RGB") # 1024 x 1024 x 3
                mask = Image.open(mask_path).convert("RGB") # 1024 x 1024 x 3

                # Apply blurring on mask to have smooth edges
                if args.blur_size >= 1:
                    blurred_img = mask.filter(ImageFilter.BoxBlur(args.blur_size))

                    blurred_img_array = np.array(blurred_img)
                    stretched_img = exposure.rescale_intensity(
                                        blurred_img_array,
                                        in_range=(127.5, 255),
                                        out_range=(0, 255))
                    mask: PIL.Image = Image.fromarray(np.uint8(stretched_img))

                # NOTE: Invert mask 
                # Because we want to keep foreground objects
                mask = Image.fromarray(255 - np.array(mask))

                # Resize segmented image, mask, and background
                image, mask_image = img.resize((H, W)), mask.resize((H, W))
                bkg: PIL.Image = bkg.resize((H, W))

                # Normalize mask
                bkg_mask = np.array(mask_image) / 255.

                # Mask out the foreground regions
                bkg = np.array(bkg)
                bkg: np.array = bkg_mask * bkg

                # Merge foreground objects and background, while in the background, the regions for
                # foreground are masked out
                new_img = np.array(image)
                new_img: np.array = np.stack((bkg, new_img)).max(axis=0)

                image: PIL.Image = Image.fromarray(new_img.astype(np.uint8))

                # Get background prompt denoting which the background scene, e.g., in the garden
                prompt: str = bkg_to_prompt[bkg_dir]

                # Define text prompt
                caption: str = CAPTION.format(prompt=prompt)

                if args.noise_bkg:
                    report: list = [
                        f"> [{idx:3d}/{len(image_ids):3d}] Create background:",
                        f"- Image id: {img_id}",
                        f"- Background image: Noise",
                        f"- Background prompt: {prompt}",
                        f"- Repainting prompt: {caption}",
                    ]
                else:
                    report: list = [
                        f"> [{idx:3d}/{len(image_ids):3d}] Create background:",
                        f"- Image id: {img_id}",
                        f"- Background image: {Path(bkg_file).stem}",
                        f"- Background prompt: {prompt}",
                        f"- Repainting prompt: {caption}",
                    ]

                md = Markdown('\n'.join(report))
                console.print(md)

                # for the cat example
                neg_prompt: str = ','.join(list(set(NEG_PROMPT.split(','))))

                # Start repainting
                images: List[PIL.Image] = pipe(
                    prompt=[caption] * args.batch_size,
                    negative_prompt=[neg_prompt] * args.batch_size,
                    image=image,
                    mask_image=mask_image,
                    guidance_scale=args.guidance_scale,
                    num_inference_steps=args.num_steps,
                    strength=args.strength,
                ).images

                # Use repainting refiner if defined
                if with_refiner:
                    images: List[PIL.Image] = refiner(
                        prompt=[caption] * args.batch_size,
                        negative_prompt=[neg_prompt] * args.batch_size,
                        image=images,
                        mask_image=mask_image,
                        guidance_scale=args.guidance_scale,
                        num_inference_steps=args.num_steps,
                        strength=args.strength,
                        denoising_start=0.75
                    ).images

                for image in images:
                    if isinstance(image, torch.Tensor):
                        image: PIL.Image = F.to_pil_image(image)

                    query: str = prompt.replace(' ', '-')

                    img_id: str = Path(img_id).stem

                    out_name = f"{img_id}_repaint-id_{cnt+1:06d}+{caption}.png"
                    image.save(osp.join(args.dest, out_name))

                    cnt += 1

                    with open(osp.join(args.ann_dir, out_ann_name), 'a+') as fp:
                        fp.write(','.join(
                                [args.dest, out_name, prompt]) + '\n')

if __name__ == "__main__":
    main()
