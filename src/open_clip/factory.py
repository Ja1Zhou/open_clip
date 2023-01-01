import json
import logging
import os
import torch
import re
from copy import deepcopy
from pathlib import Path
from typing import Optional, Tuple, Union

import torch

from .constants import OPENAI_DATASET_MEAN, OPENAI_DATASET_STD
from .model import CLIP, CustomTextCLIP, convert_weights_to_lp, convert_to_custom_text_state_dict,\
    resize_pos_embed, get_cast_dtype
from .openai import load_openai_model
from .pretrained import is_pretrained_cfg, get_pretrained_cfg, download_pretrained, list_pretrained_tags_by_model
from .transform import image_transform
from .tokenizer import HFTokenizer, tokenize


_MODEL_CONFIG_PATHS = [Path(__file__).parent / f"model_configs/"]
_MODEL_CONFIGS = {}  # directory (model_name: config) of model architecture configs


def _natural_key(string_):
    return [int(s) if s.isdigit() else s for s in re.split(r'(\d+)', string_.lower())]


def _rescan_model_configs():
    global _MODEL_CONFIGS

    config_ext = ('.json',)
    config_files = []
    for config_path in _MODEL_CONFIG_PATHS:
        if config_path.is_file() and config_path.suffix in config_ext:
            config_files.append(config_path)
        elif config_path.is_dir():
            for ext in config_ext:
                config_files.extend(config_path.glob(f'*{ext}'))

    for cf in config_files:
        with open(cf, 'r') as f:
            model_cfg = json.load(f)
            if all(a in model_cfg for a in ('embed_dim', 'vision_cfg', 'text_cfg')):
                _MODEL_CONFIGS[cf.stem] = model_cfg

    _MODEL_CONFIGS = {k: v for k, v in sorted(_MODEL_CONFIGS.items(), key=lambda x: _natural_key(x[0]))}


_rescan_model_configs()  # initial populate of model config registry


def list_models():
    """ enumerate available model architectures based on config files """
    return list(_MODEL_CONFIGS.keys())


def add_model_config(path):
    """ add model config path or file and update registry """
    if not isinstance(path, Path):
        path = Path(path)
    _MODEL_CONFIG_PATHS.append(path)
    _rescan_model_configs()


def get_model_config(model_name):
    if model_name in _MODEL_CONFIGS:
        return deepcopy(_MODEL_CONFIGS[model_name])
    else:
        return None


def get_tokenizer(model_name):
    config = get_model_config(model_name)
    tokenizer = HFTokenizer(config['text_cfg']['hf_tokenizer_name']) if 'hf_tokenizer_name' in config['text_cfg'] else tokenize
    return tokenizer


def load_state_dict(checkpoint_path: str, map_location='cpu'):
    checkpoint = torch.load(checkpoint_path, map_location=map_location)
    if isinstance(checkpoint, dict) and 'state_dict' in checkpoint:
        state_dict = checkpoint['state_dict']
    else:
        state_dict = checkpoint
    if next(iter(state_dict.items()))[0].startswith('module'):
        state_dict = {k[7:]: v for k, v in state_dict.items()}
    return state_dict


def load_checkpoint(
        model,
        checkpoint_path=None,
        strict=True,
        which_pretrained_image_tower=None,
        pretrained_image_tower=None,
    ):
    if pretrained_image_tower is not None:
        setattr(model, 'visual', pretrained_image_tower)
        return None
    
    state_dict = load_state_dict(checkpoint_path)
    # detect old format and make compatible with new format
    if 'positional_embedding' in state_dict and not hasattr(model, 'positional_embedding'):
        state_dict = convert_to_custom_text_state_dict(state_dict)

    resize_pos_embed(state_dict, model)
    
    if which_pretrained_image_tower is not None:
        state_dict = filter(lambda i: i[0].startswith("visual"), state_dict.items())
        prefix_length = len("visual")
        state_dict = map(lambda i: (i[0][prefix_length+1:], i[1]), state_dict)
        return model.visual.load_state_dict(dict(state_dict), strict=strict)

    incompatible_keys = model.load_state_dict(state_dict, strict=strict)
    return incompatible_keys

def get_cfg_and_handle_error(model_name):
    model_cfg = get_model_config(model_name)
    if model_cfg is not None:
        logging.info(f'Loaded {model_name} model config.')
    else:
        logging.error(f'Model config for {model_name} not found; available models {list_models()}.')
        raise RuntimeError(f'Model config for {model_name} not found.')
    return model_cfg

def load_and_prepare_cfg(
        model_name,
        force_quick_gelu,
        force_patch_dropout,
        pretrained_image,
        force_custom_text,
        pretrained_hf,
    ):
    '''Decouples cfg loading and updating.'''
    model_cfg = get_cfg_and_handle_error(model_name)

    # handle pretrained image
    which_pretrained_image_tower = None
    vision_cfg = model_cfg.get('vision_cfg', {})
    pretrained_image = pretrained_image or 'model_name' in vision_cfg or 'pretrained' in vision_cfg
        
    if pretrained_image:
        if 'timm_model_name' in vision_cfg:
            # pretrained weight loading for timm models set via vision_cfg
            model_cfg['vision_cfg']['timm_model_pretrained'] = True

        # elif init image tower from pre-defined and pre-trained model
        elif 'model_name' in vision_cfg:
            pretrained_image_model_name = vision_cfg.get('model_name')
            pretrained_image_model_cfg = get_cfg_and_handle_error(pretrained_image_model_name)
            model_name = pretrained_image_model_name

            which_pretrained_image_tower = vision_cfg.get('pretrained', 'openai')
            model_cfg["vision_cfg"] = pretrained_image_model_cfg["vision_cfg"]

        else:
            assert False, 'Unintended logic triggered, please debug or implement this block.'

    if force_quick_gelu:
        # override for use of QuickGELU on non-OpenAI transformer models
        model_cfg["quick_gelu"] = True

    if force_patch_dropout is not None:
        # override the default patch dropout value
        model_cfg["vision_cfg"]["patch_dropout"] = force_patch_dropout

    # for `custom_text`
    custom_text = model_cfg.pop('custom_text', False) or force_custom_text or ('hf_model_name' in model_cfg.get('text_cfg', {}))
    if custom_text:
        if 'hf_model_name' in model_cfg.get('text_cfg', {}):
            model_cfg['text_cfg']['hf_model_pretrained'] = pretrained_hf

    return model_name, model_cfg, custom_text, which_pretrained_image_tower

def create_model(
        model_name: str,
        pretrained: Optional[str] = None,
        precision: str = 'fp32',
        device: Union[str, torch.device] = 'cpu',
        jit: bool = False,
        force_quick_gelu: bool = False,
        force_custom_text: bool = False,
        force_patch_dropout: Optional[float] = None,
        pretrained_image: bool = False,
        pretrained_hf: bool = True,
        cache_dir: Optional[str] = None,
):
    model_name = model_name.replace('/', '-')  # for callers using old naming with / in ViT names

    if isinstance(device, str):
        device = torch.device(device)

    cast_dtype = get_cast_dtype(precision)

    # load and prepare model config
    model_name, model_cfg, custom_text, which_pretrained_image_tower = load_and_prepare_cfg(
        model_name=model_name,
        force_quick_gelu=force_quick_gelu,
        force_patch_dropout=force_patch_dropout,
        pretrained_image=pretrained_image,
        force_custom_text=force_custom_text,
        pretrained_hf=pretrained_hf,
    )
    
    extract_openai_image_tower = which_pretrained_image_tower is not None and which_pretrained_image_tower.lower()=='openai'
    pure_openai = pretrained and pretrained.lower() == 'openai' and not extract_openai_image_tower
    process_openai = extract_openai_image_tower or pure_openai
    pretrained_image_tower = None

    if process_openai:
        logging.info(f'Loading pretrained {model_name} from OpenAI.')
        model = load_openai_model(
            model_name,
            precision=precision,
            device=device,
            jit=jit,
            cache_dir=cache_dir,
        )
        pretrained_image_tower = getattr(model, 'visual', None) if extract_openai_image_tower else None

    if not pure_openai:
        if custom_text:
            model = CustomTextCLIP(**model_cfg, cast_dtype=cast_dtype)
        else:
            model = CLIP(**model_cfg, cast_dtype=cast_dtype)
        
        # This seems unnecessary, since only the image tower will be referenced
        # torch.cuda.empty_cache() 

        pretrained_cfg = {}
        pretrained = which_pretrained_image_tower if which_pretrained_image_tower is not None else pretrained
        checkpoint_path = None
        if pretrained and not process_openai:
            pretrained_cfg = get_pretrained_cfg(model_name, pretrained)
            if pretrained_cfg:
                checkpoint_path = download_pretrained(pretrained_cfg, cache_dir=cache_dir)
            elif os.path.exists(pretrained):
                checkpoint_path = pretrained

        if checkpoint_path or extract_openai_image_tower:
            logging.info(f'Loading pretrained {model_name} weights ({pretrained}).')
            load_checkpoint(
                model,
                checkpoint_path,
                which_pretrained_image_tower=which_pretrained_image_tower,
                pretrained_image_tower=pretrained_image_tower,
            )
        elif pretrained:
            error_str = (
                f'Pretrained weights ({pretrained}) not found for model {model_name}.'
                f'Available pretrained tags ({list_pretrained_tags_by_model(model_name)}.')
            logging.warning(error_str)
            raise RuntimeError(error_str)

        model.to(device=device)
        if precision in ("fp16", "bf16"):
            convert_weights_to_lp(model, dtype=torch.bfloat16 if precision == 'bf16' else torch.float16)

        # set image / mean metadata from pretrained_cfg if available, or use default
        model.visual.image_mean = pretrained_cfg.get('mean', None) or OPENAI_DATASET_MEAN
        model.visual.image_std = pretrained_cfg.get('std', None) or OPENAI_DATASET_STD

        if jit:
            model = torch.jit.script(model)

    return model


def create_model_and_transforms(
        model_name: str,
        pretrained: Optional[str] = None,
        precision: str = 'fp32',
        device: Union[str, torch.device] = 'cpu',
        jit: bool = False,
        force_quick_gelu: bool = False,
        force_custom_text: bool = False,
        force_patch_dropout: Optional[float] = None,
        pretrained_image: bool = False,
        pretrained_hf: bool = True,
        image_mean: Optional[Tuple[float, ...]] = None,
        image_std: Optional[Tuple[float, ...]] = None,
        cache_dir: Optional[str] = None,
):
    model = create_model(
        model_name,
        pretrained,
        precision=precision,
        device=device,
        jit=jit,
        force_quick_gelu=force_quick_gelu,
        force_custom_text=force_custom_text,
        force_patch_dropout=force_patch_dropout,
        pretrained_image=pretrained_image,
        pretrained_hf=pretrained_hf,
        cache_dir=cache_dir,
    )

    image_mean = image_mean or getattr(model.visual, 'image_mean', None)
    image_std = image_std or getattr(model.visual, 'image_std', None)
    preprocess_train = image_transform(
        model.visual.image_size,
        is_train=True,
        mean=image_mean,
        std=image_std
    )
    preprocess_val = image_transform(
        model.visual.image_size,
        is_train=False,
        mean=image_mean,
        std=image_std
    )

    return model, preprocess_train, preprocess_val


def create_model_from_pretrained(
        model_name: str,
        pretrained: str,
        precision: str = 'fp32',
        device: Union[str, torch.device] = 'cpu',
        jit: bool = False,
        force_quick_gelu: bool = False,
        force_custom_text: bool = False,
        return_transform: bool = True,
        image_mean: Optional[Tuple[float, ...]] = None,
        image_std: Optional[Tuple[float, ...]] = None,
        cache_dir: Optional[str] = None,
):
    if not is_pretrained_cfg(model_name, pretrained) and not os.path.exists(pretrained):
        raise RuntimeError(
            f'{pretrained} is not a valid pretrained cfg or checkpoint for {model_name}.'
            f' Use open_clip.list_pretrained() to find one.')

    model = create_model(
        model_name,
        pretrained,
        precision=precision,
        device=device,
        jit=jit,
        force_quick_gelu=force_quick_gelu,
        force_custom_text=force_custom_text,
        cache_dir=cache_dir,
    )

    if not return_transform:
        return model

    image_mean = image_mean or getattr(model.visual, 'image_mean', None)
    image_std = image_std or getattr(model.visual, 'image_std', None)
    preprocess = image_transform(
        model.visual.image_size,
        is_train=False,
        mean=image_mean,
        std=image_std
    )

    return model, preprocess
