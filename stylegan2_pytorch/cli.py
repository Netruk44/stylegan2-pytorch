import os
import fire
import random
import re
from retry.api import retry_call
from tqdm import tqdm
from datetime import datetime
from functools import wraps
from stylegan2_pytorch import Trainer, NanException

import torch
import torch.multiprocessing as mp
import torch.distributed as dist

import numpy as np

from azure.storage.blob import BlobServiceClient

_blob_service_client = None
_container_client = None
_delete_old_models = False
_upload_models = False
_upload_every = 10

def on_model_save(model_path):
  global _blob_service_client
  global _container_client
  global _delete_old_models
  global _upload_models
  global _upload_every

  file_path, file_name = os.path.split(model_path)
  model_name = os.path.split(file_path)[1]

  # Delete old models, leaving just what was passed in
  if _delete_old_models:
    for i in os.listdir(file_path):
      # Skip unexpected files
      if not i.startswith('model_') or not i.endswith('.pt'):
        print(f'Skipping delete of {i} (not model file)')
        continue

      # Skip the current model file
      if model_path.endswith(i):
        print(f'Skipping delete of {i} (current epoch)')
        continue
      
      print(f'Deleting {i}')
      try:
        os.remove(f'{file_path}/{i}')
      except OSError as e:
        print(f'Caught exception: {e}')

  if _upload_models:
    model_num = int(re.match(r'model_(\d+).pt', file_name).groups(1)[0])
    if (model_num % _upload_every) == 0:
      dest_path = f'{model_name}/{file_name}'
      blob_client = _container_client.get_blob_client(dest_path)

      print(f'Uploading to {dest_path}')
      with open(model_path, "rb") as data:
        blob_client.upload_blob(data)
    else:
      print('Skipping upload')

def cast_list(el):
    return el if isinstance(el, list) else [el]

def timestamped_filename(prefix = 'generated-'):
    now = datetime.now()
    timestamp = now.strftime("%m-%d-%Y_%H-%M-%S")
    return f'{prefix}{timestamp}'

def set_seed(seed):
    torch.manual_seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    np.random.seed(seed)
    random.seed(seed)

def run_training(rank, world_size, model_args, data, load_from, new, num_train_steps, name, seed):
    global _blob_service_client
    global _container_client
    global _delete_old_models
    global _upload_models
    global _upload_every
    is_main = rank == 0
    is_ddp = world_size > 1

    if is_ddp:
        set_seed(seed)
        os.environ['MASTER_ADDR'] = 'localhost'
        os.environ['MASTER_PORT'] = '12355'
        dist.init_process_group('nccl', rank=rank, world_size=world_size)

        print(f"{rank + 1}/{world_size} process initialized.")

    model_args.update(
        is_ddp = is_ddp,
        rank = rank,
        world_size = world_size
    )

    if is_main:
      if _upload_models:
        _blob_service_client = BlobServiceClient(account_url=model_args['account_url'], credential=model_args['credential'])
        _container_client = _blob_service_client.get_container_client(model_args['container_name'])

      model_args['save_callback'] = on_model_save
      _upload_models = model_args['upload_models']
      _upload_every = model_args['upload_every']
      _delete_old_models = model_args['delete_old_models']
      
    model_args.pop('upload_models', None)
    model_args.pop('upload_every', None)
    model_args.pop('delete_old_models', None)
    model_args.pop('account_url', None)
    model_args.pop('credential', None)
    model_args.pop('container_name', None)

    model = Trainer(**model_args)

    if not new:
        model.load(load_from)
    else:
        model.clear()

    model.set_data_src(data)

    progress_bar = tqdm(initial = model.steps, total = num_train_steps, mininterval=10., desc=f'{name}<{data}>')
    while model.steps < num_train_steps:
        retry_call(model.train, tries=3, exceptions=NanException)
        progress_bar.n = model.steps
        progress_bar.refresh()
        if is_main and model.steps % 50 == 0:
            model.print_log()

    model.save(model.checkpoint_num)

    if is_ddp:
        dist.destroy_process_group()

def train_from_folder(
    data = './data',
    results_dir = './results',
    models_dir = './models',
    name = 'default',
    new = False,
    load_from = -1,
    image_size = 128,
    network_capacity = 16,
    fmap_max = 512,
    transparent = False,
    batch_size = 5,
    gradient_accumulate_every = 6,
    num_train_steps = 150000,
    learning_rate = 2e-4,
    lr_mlp = 0.1,
    ttur_mult = 1.5,
    rel_disc_loss = False,
    num_workers =  None,
    save_every = 1000,
    save_callback = None,
    evaluate_every = 1000,
    evaluate_callback = None,
    generate = False,
    num_generate = 1,
    generate_interpolation = False,
    interpolation_num_steps = 100,
    save_frames = False,
    num_image_tiles = 8,
    trunc_psi = 0.75,
    mixed_prob = 0.9,
    fp16 = False,
    no_pl_reg = False,
    cl_reg = False,
    fq_layers = [],
    fq_dict_size = 256,
    attn_layers = [],
    no_const = False,
    aug_prob = 0.,
    aug_types = ['translation', 'cutout'],
    top_k_training = False,
    generator_top_k_gamma = 0.99,
    generator_top_k_frac = 0.5,
    dual_contrast_loss = False,
    dataset_aug_prob = 0.,
    multi_gpus = False,
    calculate_fid_every = None,
    calculate_fid_num_images = 12800,
    clear_fid_cache = False,
    seed = 42,
    log = False,
    lookahead=False,
    lookahead_alpha=0.5,
    lookahead_k = 5,
    ema_beta = 0.9999,

    # Vast.ai settings
    delete_old_models = True,
    upload_models = True,
    upload_every = 10,
    account_url = '',
    credential = '',
    container_name = '',
):
    global _blob_service_client
    global _container_client
    global _delete_old_models
    global _upload_models
    global _upload_every

    model_args = dict(
        name = name,
        results_dir = results_dir,
        models_dir = models_dir,
        batch_size = batch_size,
        gradient_accumulate_every = gradient_accumulate_every,
        image_size = image_size,
        network_capacity = network_capacity,
        fmap_max = fmap_max,
        transparent = transparent,
        lr = learning_rate,
        lr_mlp = lr_mlp,
        ttur_mult = ttur_mult,
        rel_disc_loss = rel_disc_loss,
        num_workers = num_workers,
        save_every = save_every,
        save_callback = save_callback,
        evaluate_every = evaluate_every,
        evaluate_callback = evaluate_callback,
        num_image_tiles = num_image_tiles,
        trunc_psi = trunc_psi,
        fp16 = fp16,
        no_pl_reg = no_pl_reg,
        cl_reg = cl_reg,
        fq_layers = fq_layers,
        fq_dict_size = fq_dict_size,
        attn_layers = attn_layers,
        no_const = no_const,
        aug_prob = aug_prob,
        aug_types = cast_list(aug_types),
        top_k_training = top_k_training,
        generator_top_k_gamma = generator_top_k_gamma,
        generator_top_k_frac = generator_top_k_frac,
        dual_contrast_loss = dual_contrast_loss,
        dataset_aug_prob = dataset_aug_prob,
        calculate_fid_every = calculate_fid_every,
        calculate_fid_num_images = calculate_fid_num_images,
        clear_fid_cache = clear_fid_cache,
        mixed_prob = mixed_prob,
        log = log,
        lookahead = lookahead,
        lookahead_alpha = lookahead_alpha,
        lookahead_k = lookahead_k,
        ema_beta = ema_beta,

        # Vast.ai settings
        delete_old_models = delete_old_models,
        upload_models = upload_models,
        upload_every = upload_every,
        account_url = account_url,
        credential = credential,
        container_name = container_name,
    )

    if generate:
        model = Trainer(**model_args)
        model.load(load_from)
        samples_name = timestamped_filename()
        for num in tqdm(range(num_generate)):
            model.evaluate(f'{samples_name}-{num}', num_image_tiles)
        print(f'sample images generated at {results_dir}/{name}/{samples_name}')
        return

    if generate_interpolation:
        model = Trainer(**model_args)
        model.load(load_from)
        samples_name = timestamped_filename()
        model.generate_interpolation(samples_name, num_image_tiles, num_steps = interpolation_num_steps, save_frames = save_frames)
        print(f'interpolation generated at {results_dir}/{name}/{samples_name}')
        return

    world_size = torch.cuda.device_count()

    if world_size == 1 or not multi_gpus:
        run_training(0, 1, model_args, data, load_from, new, num_train_steps, name, seed)
        return

    mp.spawn(run_training,
        args=(world_size, model_args, data, load_from, new, num_train_steps, name, seed),
        nprocs=world_size,
        join=True)

def main():
    fire.Fire(train_from_folder)
