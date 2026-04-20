from __future__ import annotations

import argparse

from tools.tts_common import DEFAULT_BACKEND, DEFAULT_DEVICE, resolve_backend, resolve_model_name


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prefetch the VibeVoice realtime model into the local cache.")
    parser.add_argument(
        "--backend",
        type=str,
        choices=("auto", "official", "apple"),
        default=DEFAULT_BACKEND,
        help="Which runtime to prefetch for.",
    )
    parser.add_argument("--model", type=str, help="Model path or Hugging Face repo ID.")
    parser.add_argument("--device", type=str, default=DEFAULT_DEVICE, help="Target device: mps, cuda, or cpu.")
    return parser.parse_args()


def prefetch_official(model_name: str, device: str) -> None:
    import torch

    from vibevoice.modular.modeling_vibevoice_streaming_inference import (
        VibeVoiceStreamingForConditionalGenerationInference,
    )
    from vibevoice.processor.vibevoice_streaming_processor import VibeVoiceStreamingProcessor

    if device == "mps" and not torch.backends.mps.is_available():
        device = "cpu"

    if device == "cuda":
        dtype = torch.bfloat16
        attn_impl = "flash_attention_2"
        device_map = "cuda"
    elif device == "mps":
        dtype = torch.float32
        attn_impl = "sdpa"
        device_map = None
    else:
        dtype = torch.float32
        attn_impl = "sdpa"
        device_map = "cpu"

    print(f"Prefetching processor from {model_name}")
    VibeVoiceStreamingProcessor.from_pretrained(model_name)
    print(f"Prefetching model from {model_name} on device={device} dtype={dtype} attn={attn_impl}")
    model = VibeVoiceStreamingForConditionalGenerationInference.from_pretrained(
        model_name,
        torch_dtype=dtype,
        device_map=device_map,
        attn_implementation=attn_impl,
    )
    if device == "mps":
        model.to("mps")
    print("Prefetch complete.")


def prefetch_apple(model_name: str) -> None:
    from mlx_audio.tts.utils import load_model

    print(f"Prefetching Apple MLX model from {model_name}")
    model = load_model(model_name)
    sample_rate = getattr(model, "sample_rate", "unknown")
    print(f"Prefetch complete. sample_rate={sample_rate}")


def main() -> None:
    args = parse_args()
    backend = resolve_backend(args.backend)
    model_name = resolve_model_name(backend, args.model)

    if backend == "apple":
        prefetch_apple(model_name)
        return

    prefetch_official(model_name, args.device)


if __name__ == "__main__":
    main()
