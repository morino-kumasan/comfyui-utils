import re
import random
import toml
import functools

from nodes import LoraLoader, CLIPTextEncode, ConditioningConcat

def remove_comment_out(s):
    return re.sub(r"((//|#).+$|/\*.*?\*/)", "", s).strip()

def select_dynamic_prompt(s):
    return re.sub(r"{([^}]+)}", lambda m: random.choice(m.group(1).split('|')).strip(), s)

def expand_prompt_var(d):
    def random_var(m):
        if "_v" not in d:
            print(f"_v Not Set: {d}")
            return ""
        return random.choice(d["_v"][m.group(1)])
    return re.sub(r"\${([a-zA-Z0-9_-]+)}", random_var, d["_t"])

def get_keys_all(d):
    return [k for k in d.keys() if not k.startswith("_")]

def get_keys_all_recursive(d, prefix=[]):
    r = []
    if get_keys_all(d) == 0:
        return [".".join(prefix)]
    for k, v in d.items():
        if k.startswith("_"):
            continue
        if "_t" in v:
            r += ['.'.join(prefix + [k])]
        r += get_keys_all_recursive(v, prefix + [k])
    return r

def get_keys_random(d):
    rand_keys = get_keys_all(d)
    return random.choice(rand_keys)

def get_keys_random_recursive(d):
    rand_keys = get_keys_all_recursive(d)
    return random.choice(rand_keys)

def build_search_keys(keys, prefix=[]):
    assert len(keys) > 0
    if isinstance(keys, str):
        keys = [(key.split("+")) for key in keys.split(".")]
    if len(keys) == 1:
        return [".".join(prefix + [key]) for key in keys[0]]
    return functools.reduce(lambda x, y: x + y, [[".".join(prefix + [key])] + build_search_keys(keys[1:], prefix + [key]) for key in keys[0]])

def collect_prompt(prompt_dict, keys, exclude_keys=None, init_prefix=None):
    if isinstance(keys, str):
        keys = build_search_keys(keys)

    if exclude_keys is None:
        exclude_keys = []

    r = []
    for key in keys:
        d = prompt_dict
        key_parts = key.split(".")
        prefix = init_prefix or []
        while len(key_parts) > 0:
            key = key_parts.pop(0)
            if key == "?":
                key = get_keys_random(d)
            elif key == "??":
                assert len(key_parts) == 0
                keys = get_keys_random_recursive(d)
                r += collect_prompt(d, keys, exclude_keys, prefix[:])
                break
            elif key == "*":
                keys = [".".join([key] + key_parts) for key in get_keys_all(d)]
                r += collect_prompt(d, keys, exclude_keys, prefix[:])
                break
            elif key == "**":
                assert len(key_parts) == 0
                keys = get_keys_all_recursive(d)
                r += collect_prompt(d, keys, exclude_keys, prefix[:])
                break

            if key not in d:
                print(f"Key Not Found: {'.'.join(prefix + [key])}")
                break
            d = d[key]
            prefix += [key]
        else:
            prefix_str = ".".join(prefix)
            if "_t" in d:
                if prefix_str not in exclude_keys:
                    r += [select_dynamic_prompt(remove_comment_out(expand_prompt_var(d)))]
                    exclude_keys += [prefix_str]
            else:
                print(f"Key Not Found: {prefix_str}")
    return r

class TomlPromptEncoder:
    RETURN_TYPES = ("MODEL", "CLIP", "CONDITIONING", "STRING", "STRING", "INT")
    OUTPUT_TOOLTIPS = ("The diffusion model.", "The CLIP model.", "A Conditioning containing a text by key_name.", "Loaded LoRA name list", "A prompt", "Random seed")
    FUNCTION = "load_prompt"
    CATEGORY = "conditioning"
    DESCRIPTION = "LoRA prompt load."

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": ("MODEL", {"tooltip": "The diffusion model."}),
                "clip": ("CLIP", {"tooltip": "The CLIP model."}),
                "key_name_list": ("STRING", {"multiline": True, "dynamicPrompts": True, "tooltip": "Select Key Name"}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff, "tooltip": "Random seed."}),
                "text": ("STRING", {"multiline": True, "dynamicPrompts": True, "defaultInput": True, "tooltip": "TOML format prompt."}),
                "lora_info": ("STRING", {"multiline": True, "dynamicPrompts": True, "defaultInput": True, "tooltip": "TOML format lora prompt."}),
            }
        }

    def __init__(self):
        self.encoder = CLIPTextEncode()
        self.concat = ConditioningConcat()
        self.loader = {}
        self.loras = []
        self.prompt = []
        self.loaded_keys = []

    def load_lora_from_prompt(self, prompt, lora_dict, model, clip):
        r_model = model
        r_clip = clip
        for lora_name, strength in re.findall(r'<lora:([^:]+):([0-9.]+)>', prompt):
            if lora_name not in self.loader:
                self.loader[lora_name] = LoraLoader()
                r_model, r_clip = self.loader[lora_name].load_lora(r_model, r_clip, lora_name, float(strength), float(strength))
                print(f"Lora Loaded: {lora_name}: {strength}")
            self.loras += ["<lora:{}:{}>".format(lora_name, strength)]
        prompt = re.sub(r'<lora:([^:]+):([0-9.]+)>', lambda m: ','.join(collect_prompt(lora_dict, [m.group(1).replace("\\", "\\\\")])), prompt)
        return (r_model, r_clip, prompt)

    def encode_prompt(self, prompt, lora_dict, model, clip, cond):
        r_model = model
        r_clip = clip
        r_cond = cond
        prompt = prompt.strip()
        if prompt == "":
            return (r_model, r_clip, r_cond)

        r_model, r_clip, prompt = self.load_lora_from_prompt(prompt, lora_dict, r_model, r_clip)
        self.prompt += [prompt]

        cond = self.encoder.encode(r_clip, prompt)[0]
        if r_cond is None:
            r_cond = cond
        else:
            r_cond = self.concat.concat(cond, r_cond)[0]
        return (r_model, r_clip, r_cond)

    def load_prompt(self, model, clip, seed, text, lora_info, key_name_list):
        random.seed(seed)
        self.loader = {}
        self.loras = []
        self.prompt = []
        self.loaded_keys = []

        r_cond = None
        r_model = model
        r_clip = clip
        prompt_dict = toml.loads(text)
        lora_dict = toml.loads(lora_info)
        for key_str in key_name_list.splitlines():
            key_str = select_dynamic_prompt(remove_comment_out(key_str))
            if key_str == "":
                continue

            prompts = []
            for key in [k.strip() for k in key_str.split("&")]:
                m = re.match(r'^<lora:([^:]+):([0-9.]+)>$', key)
                if m:
                    r_model, r_clip, prompt = self.load_lora_from_prompt(key, lora_dict, r_model, r_clip)
                    prompts += [prompt]
                else:
                    prompts += [','.join(collect_prompt(prompt_dict, build_search_keys(key), exclude_keys=self.loaded_keys))]
            prompt = ','.join(prompts)

            r_model, r_clip, r_cond = self.encode_prompt(prompt, lora_dict, r_model, r_clip, r_cond)

        if r_cond is None:
            r_cond = self.encoder.encode(clip, "")[0]

        return (r_model, r_clip, r_cond, '\n'.join(self.loras), '\nBREAK\n'.join([p for p in self.prompt if p]), seed)