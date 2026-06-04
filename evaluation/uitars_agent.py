#!/usr/bin/env python3
"""
uitars_agent.py
Wraps UI-TARS (Qwen2.5-VL base or LoRA fine-tuned) for WebArena evaluation.

Input:  PIL screenshot + task intent
Output: WebArena playwright action string
"""
import re
from typing import Optional
import torch
from PIL import Image


class UITARSAgent:
    """
    UI-TARS inference wrapper.

    Coordinate conversion
    ─────────────────────
    Our training screenshots are 1920×878.
    UI-TARS outputs normalised coordinates in [0, 1000].
    At eval time we set the WebArena viewport to 1920×878 so the
    coordinate mapping stays identical to training.

    Action conversion (UI-TARS → playwright)
    ──────────────────────────────────────────
    click(start_box='(xn,yn)')                → page.mouse.click(X, Y)
    type(content='text')                      → page.keyboard.type('text')
    scroll(start_box='(xn,yn)',               → page.mouse.move(X, Y)
           direction='down', step_count=N)       page.mouse.wheel(0, DY)
    stop(content='answer')                    → page.stop('answer')
    """

    VIEWPORT_W    = 1920   # must match training screenshot width
    VIEWPORT_H    = 878    # must match training screenshot height
    MAX_NEW_TOKENS = 128

    def __init__(self, model_path: str, lora_path: Optional[str] = None):
        from transformers import Qwen2_5_VLForConditionalGeneration, AutoProcessor

        print(f"Loading tokeniser / processor from {model_path} …")
        self.processor = AutoProcessor.from_pretrained(
            model_path, trust_remote_code=True
        )

        print("Loading model weights …")
        self.model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
            model_path,
            torch_dtype=torch.bfloat16,
            device_map="auto",
            trust_remote_code=True,
        )

        if lora_path:
            print(f"Merging LoRA adapters from {lora_path} …")
            from peft import PeftModel
            self.model = PeftModel.from_pretrained(self.model, lora_path)
            self.model = self.model.merge_and_unload()

        self.model.eval()
        print("Agent ready.\n")

    # ──────────────────────────────────────────────────────────────────────
    # Inference
    # ──────────────────────────────────────────────────────────────────────

    def predict(
        self,
        screenshot: Image.Image,
        intent: str,
        needs_answer: bool = False,
    ) -> str:
        """
        Run one forward pass and return the raw model output string.

        Args:
            screenshot:   Current browser screenshot (PIL Image).
            intent:       Task description.
            needs_answer: If True (string_match tasks), adds a hint to
                          produce stop(content='answer') when done.
        """
        prompt = (
            "You are a web browser agent. Complete the following task.\n"
            f"Task: {intent}\n"
            "What is the next action?"
        )
        if needs_answer:
            prompt += (
                "\nIf you have found the answer, "
                "output: stop(content='your answer here')"
            )

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image",  "image": screenshot},
                    {"type": "text",   "text":  prompt},
                ],
            }
        ]

        text = self.processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        inputs = self.processor(
            text=[text],
            images=[screenshot],
            return_tensors="pt",
            padding=True,
        )
        inputs = {k: v.to("cuda") for k, v in inputs.items()}

        with torch.no_grad():
            output_ids = self.model.generate(
                **inputs,
                max_new_tokens=self.MAX_NEW_TOKENS,
                do_sample=False,
                temperature=None,
                top_p=None,
            )

        new_tokens = output_ids[0][inputs["input_ids"].shape[1]:]
        return self.processor.decode(new_tokens, skip_special_tokens=True).strip()

    # ──────────────────────────────────────────────────────────────────────
    # Action conversion
    # ──────────────────────────────────────────────────────────────────────

    def to_playwright(self, response: str) -> str:
        """Convert a UI-TARS action string to a WebArena playwright string."""
        text = response.strip()
        # Strip optional "Action: " prefix
        if text.lower().startswith("action:"):
            text = text[7:].strip()

        # stop(content='answer')
        m = re.match(r"stop\(content='(.*)'\)\s*$", text, re.DOTALL)
        if m:
            ans = m.group(1).replace("'", "\\'")
            return f"page.stop('{ans}')"

        # click(start_box='(xn,yn)')
        m = re.match(r"click\(start_box='?\((\d+),\s*(\d+)\)'?\)", text)
        if m:
            xn, yn = int(m.group(1)), int(m.group(2))
            x = round(xn / 1000 * self.VIEWPORT_W)
            y = round(yn / 1000 * self.VIEWPORT_H)
            return f"page.mouse.click({x}, {y})"

        # type(content='...')
        m = re.match(r"type\(content='(.*)'\)\s*$", text, re.DOTALL)
        if m:
            content = m.group(1).replace("\\", "\\\\").replace("'", "\\'")
            return f"page.keyboard.type('{content}')"

        # scroll(start_box='(xn,yn)', direction='up|down', step_count=N)
        m = re.match(
            r"scroll\(start_box='?\((\d+),\s*(\d+)\)'?,\s*"
            r"direction='(up|down)',\s*step_count=(\d+)\)",
            text,
        )
        if m:
            xn, yn   = int(m.group(1)), int(m.group(2))
            direction = m.group(3)
            steps     = int(m.group(4))
            x  = round(xn / 1000 * self.VIEWPORT_W)
            y  = round(yn / 1000 * self.VIEWPORT_H)
            dy = steps * 400 * (1 if direction == "down" else -1)
            # Two-line playwright: move then wheel
            return f"page.mouse.move({x}, {y})\npage.mouse.wheel(0, {dy})"

        # Unrecognised → no-op
        print(f"  [WARN] Unrecognised action: {text!r} — skipping")
        return "page.keyboard.press('Escape')"
