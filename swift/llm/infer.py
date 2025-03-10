# Copyright (c) Alibaba, Inc. and its affiliates.
import datetime as dt
import os
import shutil
from typing import Any, Dict, Literal, Optional, Tuple

import json
import numpy as np
import torch
from modelscope import BitsAndBytesConfig, GenerationConfig
from tqdm import tqdm
from transformers import PreTrainedModel, PreTrainedTokenizerBase
from transformers.utils import is_torch_npu_available

from swift.tuners import Swift
from swift.utils import (append_to_jsonl, get_logger, get_main, get_model_info,
                         read_multi_line, seed_everything, show_layers)
from .utils import (InferArguments, Template, get_additional_saved_files,
                    get_dataset, get_model_tokenizer, get_template, inference,
                    inference_stream, is_adapter, set_generation_config)

logger = get_logger()


def save_checkpoint(model: Optional[PreTrainedModel],
                    tokenizer: PreTrainedTokenizerBase,
                    model_cache_dir: str,
                    ckpt_dir: Optional[str],
                    target_dir: str,
                    *,
                    save_safetensors: bool = True) -> None:
    if model is not None:
        model.save_pretrained(target_dir, safe_serialization=save_safetensors)
    tokenizer.save_pretrained(target_dir)
    model_type = getattr(tokenizer, 'model_type')
    fname_list = ['generation_config.json', 'preprocessor_config.json']
    if model_type is not None:
        fname_list += get_additional_saved_files(model_type)

    for fname in fname_list:
        tgt_path = os.path.join(target_dir, fname)
        for model_dir in [ckpt_dir, model_cache_dir]:
            if model_dir is None:
                continue
            src_path = os.path.join(model_dir, fname)
            if os.path.isfile(src_path):
                shutil.copy(src_path, tgt_path)
                break
            elif os.path.isdir(src_path):
                shutil.copytree(src_path, tgt_path)
                break
    # configuration.json
    configuration_fname = 'configuration.json'
    new_configuration_path = os.path.join(target_dir, configuration_fname)
    for model_dir in [ckpt_dir, model_cache_dir]:
        if model_dir is None:
            continue
        old_configuration_path = os.path.join(model_dir, configuration_fname)
        if os.path.exists(old_configuration_path):
            with open(old_configuration_path, 'r', encoding='utf-8') as f:
                res = json.load(f)
            res.pop('adapter_cfg', None)
            with open(new_configuration_path, 'w', encoding='utf-8') as f:
                json.dump(res, f, ensure_ascii=False, indent=4)
            break
    if ckpt_dir is not None:
        # sft_args.json
        sft_args_fname = 'sft_args.json'
        old_sft_args_path = os.path.join(ckpt_dir, sft_args_fname)
        new_sft_args_path = os.path.join(target_dir, sft_args_fname)
        if os.path.exists(old_sft_args_path):
            with open(old_sft_args_path, 'r', encoding='utf-8') as f:
                res = json.load(f)
            res['sft_type'] = 'full'
            with open(new_sft_args_path, 'w', encoding='utf-8') as f:
                json.dump(res, f, ensure_ascii=False, indent=2)


def merge_lora(args: InferArguments,
               replace_if_exists=False,
               device_map: Optional[str] = None,
               **kwargs) -> Optional[str]:
    logger.info(f'replace_if_exists: {replace_if_exists}')
    assert args.ckpt_dir is not None, 'args.ckpt_dir is not specified.'
    assert args.sft_type == 'lora', "Only supports sft_type == 'lora'"
    for s in ['int4', 'int8', 'awq']:
        assert s not in args.model_type, f'{s} model is not supported'
    if args.quantization_bit != 0:
        logger.warning('It is not recommended to merge quantized models, '
                       'as this can result in performance degradation')
    ckpt_dir, ckpt_name = os.path.split(args.ckpt_dir)
    merged_lora_path = os.path.join(ckpt_dir, f'{ckpt_name}-merged')
    logger.info(f'merged_lora_path: `{merged_lora_path}`')
    if os.path.exists(merged_lora_path) and not replace_if_exists:
        logger.info(
            f'The weight directory for the merged LoRA already exists in {args.ckpt_dir}, '
            'skipping the saving process. '
            'you can pass `replace_if_exists=True` to overwrite it.')
    else:
        model, template = prepare_model_template(
            args, device_map=args.merge_device_map, verbose=False)
        logger.info('Merge LoRA...')
        Swift.merge_and_unload(model)
        model = model.model
        logger.info('Saving merged weights...')
        save_checkpoint(
            model,
            template.tokenizer,
            model.model_dir,
            args.ckpt_dir,
            merged_lora_path,
            save_safetensors=args.save_safetensors)
        logger.info(
            f'Successfully merged LoRA and saved in {merged_lora_path}.')
    logger.info("Setting args.sft_type: 'full'")
    logger.info(f'Setting args.ckpt_dir: {merged_lora_path}')
    args.sft_type = 'full'
    args.ckpt_dir = merged_lora_path
    return merged_lora_path


def prepare_model_template(
        args: InferArguments,
        *,
        device_map: Optional[str] = None,
        verbose: bool = True,
        automodel_class=None) -> Tuple[PreTrainedModel, Template]:

    model_kwargs = {}
    if is_torch_npu_available():
        logger.info(f'device_count: {torch.npu.device_count()}')
        if device_map is None:
            device_map = 'npu:0'
    else:
        logger.info(f'device_count: {torch.cuda.device_count()}')
        if device_map is None:
            device_map = 'auto'
    if device_map == 'auto':
        model_kwargs['low_cpu_mem_usage'] = True
    model_kwargs['device_map'] = device_map

    # Loading Model and Tokenizer
    if args.load_in_8bit or args.load_in_4bit:
        quantization_config = BitsAndBytesConfig(
            args.load_in_8bit,
            args.load_in_4bit,
            bnb_4bit_compute_dtype=args.bnb_4bit_compute_dtype,
            bnb_4bit_quant_type=args.bnb_4bit_quant_type,
            bnb_4bit_use_double_quant=args.bnb_4bit_use_double_quant)
        if args.bnb_4bit_compute_dtype is None:
            quantization_config.bnb_4bit_compute_dtype = None
        logger.info(f'quantization_config: {quantization_config.__dict__}')
        model_kwargs['quantization_config'] = quantization_config
    kwargs = {}
    if args.use_flash_attn is not None:
        kwargs['use_flash_attn'] = args.use_flash_attn
    model_id_or_path = None
    if args.sft_type == 'full' and args.ckpt_dir is not None:
        model_id_or_path = args.ckpt_dir
    elif args.model_id_or_path is not None:
        model_id_or_path = args.model_id_or_path
    if automodel_class is not None:
        kwargs['automodel_class'] = automodel_class

    model, tokenizer = get_model_tokenizer(
        args.model_type,
        args.torch_dtype,
        model_kwargs,
        model_id_or_path=model_id_or_path,
        revision=args.model_revision,
        **kwargs)
    if verbose:
        logger.info(f'model_config: {model.config}')

    generation_config = GenerationConfig(
        max_new_tokens=args.max_new_tokens,
        temperature=args.temperature,
        top_k=args.top_k,
        top_p=args.top_p,
        do_sample=args.do_sample,
        repetition_penalty=args.repetition_penalty,
        num_beams=args.num_beams,
        pad_token_id=tokenizer.pad_token_id,
        eos_token_id=tokenizer.eos_token_id)
    logger.info(f'generation_config: {generation_config}')
    set_generation_config(model, generation_config)

    if model.max_model_len is None:
        model.max_model_len = args.max_model_len
    elif args.max_model_len is not None:
        if args.max_model_len <= model.max_model_len:
            model.max_model_len = args.max_model_len
        else:
            raise ValueError(
                'args.max_model_len exceeds the maximum max_model_len supported by the model.'
                f'args.max_model_len: {args.max_model_len}, model.max_model_len: {model.max_model_len}'
            )
    # Preparing LoRA
    if is_adapter(args.sft_type) and args.ckpt_dir is not None:
        model = Swift.from_pretrained(
            model, args.ckpt_dir, inference_mode=True)
        if args.sft_type == 'adalora':
            model = model.to(model.dtype)

    if verbose:
        show_layers(model)
        logger.info(model)
    logger.info(get_model_info(model))

    template: Template = get_template(
        args.template_type,
        tokenizer,
        args.system,
        args.max_length,
        args.truncation_strategy,
        model=model)
    args.system = template.default_system
    logger.info(f'system: {args.system}')
    return model, template


def read_media_file(
        infer_kwargs: Dict[str, Any],
        infer_media_type: Literal['none', 'round', 'dialogue']) -> None:
    text = 'Input a media path or URL <<< '
    images = infer_kwargs.get('images', [])
    if infer_media_type == 'none':
        return
    if infer_media_type == 'round' or len(images) == 0:
        image = input(text)
        if len(image) > 0:
            images += [image]
    if len(images) > 0:
        infer_kwargs['images'] = images


def llm_infer(args: InferArguments) -> None:
    logger.info(f'args: {args}')
    seed_everything(args.seed)
    if args.merge_lora:
        merge_lora(args, device_map=args.merge_device_map)
    if args.infer_backend == 'vllm':
        from .utils import prepare_vllm_engine_template, inference_stream_vllm, inference_vllm
        llm_engine, template = prepare_vllm_engine_template(args)
    else:
        model, template = prepare_model_template(args)
        if args.overwrite_generation_config:
            assert args.ckpt_dir is not None, 'args.ckpt_dir is not specified.'
            model.generation_config.save_pretrained(args.ckpt_dir)
    lora_request = None
    if args.vllm_enable_lora:
        assert len(args.vllm_lora_request_list) == 1
        lora_request = args.vllm_lora_request_list[0]
    # Inference
    result = []
    jsonl_path = None
    if args.save_result:
        result_dir = args.ckpt_dir
        if result_dir is None:
            result_dir = llm_engine.model_dir if args.infer_backend == 'vllm' else model.model_dir
        if result_dir is not None:
            result_dir = os.path.join(result_dir, 'infer_result')
            os.makedirs(result_dir, exist_ok=True)
            time = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
            jsonl_path = os.path.join(result_dir, f'{time}.jsonl')
    if args.eval_human:
        input_mode: Literal['S', 'M'] = 'S'
        logger.info('Input `exit` or `quit` to exit the conversation.')
        logger.info('Input `multi-line` to switch to multi-line input mode.')
        logger.info(
            'Input `reset-system` to reset the system and clear the history.')
        if template.support_multi_round:
            logger.info('Input `clear` to clear the history.')
        else:
            logger.info(
                'The current template only supports single-round dialogues.')
        history = []
        infer_kwargs = {}
        if args.infer_media_type != 'none':
            logger.info('Please enter the conversation content first, '
                        'followed by the path to the multimedia file.')
        system = None
        read_system = False
        while True:
            if input_mode == 'S':
                addi_prompt = ''
                if read_system:
                    addi_prompt = '[S]'
                query = input(f'<<<{addi_prompt} ')
            else:
                addi_prompt = '[M]'
                if read_system:
                    addi_prompt = '[MS]'
                query = read_multi_line(addi_prompt)
            if query.strip().lower() in {'exit', 'quit'}:
                break
            elif query.strip().lower() == 'clear':
                history = []
                infer_kwargs = {}
                continue
            elif query.strip() == '':
                continue
            elif query.strip().lower() == 'reset-system':
                read_system = True
                continue
            if read_system:
                system = query
                read_system = False
                continue
            if input_mode == 'S' and query.strip().lower() == 'multi-line':
                input_mode = 'M'
                logger.info('End multi-line input with `#`.')
                logger.info(
                    'Input `single-line` to switch to single-line input mode.')
                continue
            if input_mode == 'M' and query.strip().lower() == 'single-line':
                input_mode = 'S'
                continue
            if not template.support_multi_round:
                history = []
                infer_kwargs = {}

            read_media_file(infer_kwargs, args.infer_media_type)
            if args.infer_backend == 'vllm':
                request_list = [{
                    'query': query,
                    'history': history,
                    'system': system
                }]
                if args.stream:
                    gen = inference_stream_vllm(
                        llm_engine,
                        template,
                        request_list,
                        lora_request=lora_request)
                    print_idx = 0
                    for resp_list in gen:
                        response = resp_list[0]['response']
                        new_history = resp_list[0]['history']
                        if len(response) > print_idx:
                            print(response[print_idx:], end='', flush=True)
                            print_idx = len(response)
                    print()
                else:
                    resp_list = inference_vllm(
                        llm_engine,
                        template,
                        request_list,
                        lora_request=lora_request)
                    response = resp_list[0]['response']
                    new_history = resp_list[0]['history']
                    print(response)
            else:
                if args.stop_words:
                    infer_kwargs['stop_words'] = args.stop_words
                if args.stream:
                    gen = inference_stream(model, template, query, history,
                                           system, **infer_kwargs)
                    print_idx = 0
                    for response, new_history in gen:
                        if len(response) > print_idx:
                            print(response[print_idx:], end='', flush=True)
                            print_idx = len(response)
                    print()
                else:
                    response, new_history = inference(model, template, query,
                                                      history, system,
                                                      **infer_kwargs)
                    print(response)
            print('-' * 50)
            obj = {
                'query': query,
                'response': response,
                'history': history,
            }
            history = new_history
            if jsonl_path is not None:
                append_to_jsonl(jsonl_path, obj)
            result.append(obj)
    else:
        random_state = np.random.RandomState(args.dataset_seed)
        _, val_dataset = get_dataset(
            args.dataset,
            args.dataset_test_ratio,
            random_state,
            check_dataset_strategy=args.check_dataset_strategy)
        if args.val_dataset_sample >= 0 and val_dataset.shape[
                0] > args.val_dataset_sample:
            logger.info(f'val_dataset_sample: {args.val_dataset_sample}')
            val_idxs = random_state.permutation(args.val_dataset_sample)
            val_dataset = val_dataset.select(val_idxs)

        logger.info(f'val_dataset: {val_dataset}')
        if args.verbose is None:
            if len(val_dataset) >= 100:
                args.verbose = False
            else:
                args.verbose = True
            logger.info(f'Setting args.verbose: {args.verbose}')
        if not args.verbose and args.stream:
            args.stream = False
            logger.info(f'Setting args.stream: {args.stream}')

        if args.infer_backend == 'vllm' and not args.stream:
            if args.verbose:
                args.verbose = False
                logger.info('Setting args.verbose: False')
            label_list = None
            if 'response' in val_dataset.features:
                label_list = val_dataset['response']
            val_dataset = val_dataset.remove_columns('response')
            request_list = val_dataset.to_list()
            resp_list = inference_vllm(
                llm_engine, template, request_list, use_tqdm=True)
            result = []
            if label_list is not None:
                for request, label in zip(request_list, label_list):
                    request['label'] = label
            for request, resp in zip(request_list, resp_list):
                obj = {'response': resp['response'], **request}
                if jsonl_path is not None:
                    append_to_jsonl(jsonl_path, obj)
                result.append(obj)
        else:
            if not args.verbose:
                val_dataset = tqdm(val_dataset)
            for data in val_dataset:
                kwargs = {'query': data['query']}
                history = data.get('history')
                system = data.get('system')
                images = data.get('images')
                if args.verbose and system is not None:
                    print(f'[SYSTEM]{system}')
                if history is not None:
                    kwargs['history'] = history
                if system is not None:
                    kwargs['system'] = system
                if images is not None:
                    kwargs['images'] = images
                if args.infer_backend == 'vllm':
                    assert args.stream is True
                    if args.verbose:
                        print(f"[QUERY]{data['query']}\n[RESPONSE]", end='')
                    gen = inference_stream_vllm(
                        llm_engine,
                        template, [kwargs],
                        lora_request=lora_request)
                    print_idx = 0
                    for resp_list in gen:
                        response = resp_list[0]['response']
                        if args.verbose and len(response) > print_idx:
                            print(response[print_idx:], end='', flush=True)
                            print_idx = len(response)
                    print()
                else:
                    response, _ = inference(
                        model,
                        template,
                        stream=args.stream and args.verbose,
                        verbose=args.verbose,
                        **kwargs)
                label = data.pop('response')
                if label is not None:
                    kwargs['label'] = label
                obj = {'response': response, **kwargs}
                if jsonl_path is not None:
                    append_to_jsonl(jsonl_path, obj)
                result.append(obj)
                if args.verbose:
                    print()
                    print(f'[LABELS]{label}')
                    if images is not None:
                        print(f'[IMAGES]{images}')
                    print('-' * 50)
    if jsonl_path is not None:
        logger.info(f'save_result_path: {jsonl_path}')
    if args.val_dataset_sample == 10:  # is default
        logger.info(
            'You can set `--val_dataset_sample -1` to perform inference on the entire dataset.'
        )
    return {'result': result}


infer_main = get_main(InferArguments, llm_infer)
merge_lora_main = get_main(InferArguments, merge_lora)
