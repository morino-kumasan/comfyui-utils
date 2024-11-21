import os
import re
import random
import tomllib
import functools

def remove_comment_out(s):
    return re.sub(r"((//|#).+$|/\*[\s\S]*?\*/)", "", s, flags=re.MULTILINE).strip()

def select_dynamic_prompt(s):
    return re.sub(r"{([^}]+)}", lambda m: random.choice(m.group(1).split('|')).strip(), s, flags=re.MULTILINE)

def split_toml_prompt_line(s):
    r = []
    beg = 0
    for m in re.finditer(r'<[^>]+>', s):
        span = m.span()
        if beg < span[0]:
            r += [v.strip() for v in s[beg:span[0]].split(",")]
        r += [m.group(0)]
        beg = span[1]
    if beg < len(s):
        r += [v.strip() for v in s[beg:len(s)].split(",")]
    return [v for v in r if v]

def expand_prompt_var(d, global_vars):
    def random_var(m):
        var_name = m.group(1)
        if var_name.startswith("g."):
            var_name = var_name[2:]
            vars = global_vars
        else:
            vars = d.get("_v", None)
            if vars is None:
                print(f"_v Not Set: {d}")
                return ""
        return random.choice(vars[var_name])
    return re.sub(r"\${([a-zA-Z0-9_.]+)}", random_var, d if isinstance(d, str) else d["_t"], flags=re.MULTILINE)

def get_keys_all(d):
    return [k for k in d.keys() if not k.startswith("_")]

def get_keys_all_recursive(d, prefix=[]):
    r_long = []
    r_short = []
    for k, v in [(k, v) for k, v in d.items() if not k.startswith("_")]:
        if isinstance(v, str):
            r_long += ['.'.join(prefix + [k])]
        elif len(get_keys_all(v)) == 0:
            if "_t" in v:
                r_long += ['.'.join(prefix + [k])]
        else:
            if "_t" in v:
                r_short += ['.'.join(prefix + [k])]
            l, s = get_keys_all_recursive(v, prefix + [k])
            r_long += l
            r_short += s
    return (r_long, r_short)

def get_keys_random(d):
    rand_keys = get_keys_all(d)
    return random.choice(rand_keys)

def get_keys_random_recursive(d):
    rand_keys, keys = get_keys_all_recursive(d)
    selected_key = random.choice(rand_keys)
    return [key for key in keys if selected_key.startswith(f"{key}.")] + [selected_key]

def build_search_keys(keys, prefix=[]):
    if isinstance(keys, str):
        keys = [(key.split("+")) for key in keys.split(".")]
    key_len = len(keys)
    if key_len == 1:
        return [".".join(prefix + [key]) for key in keys[0]]
    elif key_len == 0:
        return []
    return functools.reduce(lambda x, y: x + y, [[".".join(prefix + [key])] + build_search_keys(keys[1:], prefix + [key]) for key in keys[0]])

def collect_prompt(prompt_dict, keys, exclude_keys=None, init_prefix=None, global_vars=None, ignore_split=False):
    if isinstance(keys, str):
        keys = build_search_keys(keys)

    if exclude_keys is None:
        exclude_keys = []
    if global_vars is None:
        global_vars = prompt_dict.get("_v", {})

    r = []
    for key in keys:
        d = prompt_dict
        key_parts = key.split(".") if not ignore_split else [key]
        prefix = init_prefix[:] if init_prefix is not None else []
        while len(key_parts) > 0:
            key = key_parts.pop(0)
            if key == "?":
                key = get_keys_random(d)
            elif key == "??":
                assert len(key_parts) == 0
                keys = get_keys_random_recursive(d)
                r += collect_prompt(d, keys, exclude_keys, prefix, global_vars)
                break
            elif key == "*":
                keys = [".".join([key] + key_parts) for key in get_keys_all(d)]
                r += collect_prompt(d, keys, exclude_keys, prefix, global_vars)
                break
            elif key == "**":
                assert len(key_parts) == 0
                keys = get_keys_all_recursive(d)
                r += collect_prompt(d, keys[1] + keys[0], exclude_keys, prefix, global_vars)
                break

            if key not in d:
                break
            d = d[key]
            prefix += [key]
        else:
            prefix_str = ".".join(prefix)
            d_is_str = isinstance(d, str)
            if d_is_str or "_t" in d:
                if prefix_str not in exclude_keys:
                    r += [select_dynamic_prompt(remove_comment_out(expand_prompt_var(d, global_vars)))]
                    exclude_keys += [prefix_str]
                    print(f"Load Prompt: {prefix_str}")
                elif d_is_str or len(get_keys_all(d)) == 0:
                    r += [select_dynamic_prompt(remove_comment_out(expand_prompt_var(d, global_vars)))]
                    print(f"Load Prompt (Duplicated): {prefix_str}")
    return r

def expand_prompt_tag_lora(prompt, d):
    def lora_prompt(m):
        # for toml key
        lora_name = m.group(1).replace(os.path.sep, "/")
        return ','.join(collect_prompt(d, [lora_name], ignore_split=True))
    return re.sub(r'<lora:([^:]+):([0-9.]+)>', lora_prompt, prompt, flags=re.MULTILINE)

def expand_prompt_tag_negative(prompt):
    return re.sub(r'<!:([^>]+)>', '', prompt, flags=re.MULTILINE)

def expand_prompt_tag_positive(prompt):
    return re.sub(r'<raw:([^>]+)>', lambda m: m.group(1), prompt, flags=re.MULTILINE)

def expand_prompt_tag_if(prompt, prompt_dict, loaded_keys, loras):
    def cond(m):
        keys = [v.strip() for v in m.group(1).split(",")]
        if len([v for v in keys if v not in loaded_keys]) == 0:
            r = load_prompt_line(m.group(2), prompt_dict, loaded_keys, loras)
            return r[0]
        else:
            r = load_prompt_line(m.group(3), prompt_dict, loaded_keys, loras)
            return r[0]
    return re.sub(r'<if:([^:]+):([^:]*):([^>]*)>', cond, prompt, flags=re.MULTILINE)

def expand_prompt_tag_if_not(prompt, prompt_dict, loaded_keys, loras):
    def cond(m):
        keys = [v.strip() for v in m.group(1).split(",")]
        if len([v for v in keys if v in loaded_keys]) == 0:
            r = load_prompt_line(m.group(2), prompt_dict, loaded_keys, loras)
            return r[0]
        else:
            r = load_prompt_line(m.group(3), prompt_dict, loaded_keys, loras)
            return r[0]
    return re.sub(r'<if_not:([^:]+):([^:]*):([^>]*)>', cond, prompt, flags=re.MULTILINE)

def expand_prompt_tag(prompt, prompt_dict, loaded_keys, loras):
    negative = []
    for tag, args in re.findall(r'<([^:]+):([^>]+)>', prompt, flags=re.MULTILINE):
        if tag == "lora":
            lora_name, strength = args.split(":")
            lora_tag = "<lora:{}:{}>".format(lora_name, strength)
            if lora_tag not in loras:
                loras += [lora_tag]
                loaded_keys += [lora_name]
        elif tag == "!":
            negative += [args]

    positive = expand_prompt_tag_positive(prompt)
    positive = expand_prompt_tag_negative(positive)
    positive = expand_prompt_tag_if(positive, prompt_dict, loaded_keys, loras)
    positive = expand_prompt_tag_if_not(positive, prompt_dict, loaded_keys, loras)
    positive = expand_prompt_tag_lora(positive, prompt_dict.get("<lora>", {}))

    negative = ",".join(negative)
    return (positive, negative)

def load_prompt_line(s, prompt_dict, loaded_keys, loras):
    prompts = []
    for key in split_toml_prompt_line(s):
        m = re.match(r'^<([^:]+):([^>]+)>$', key)
        if m:
            # tag
            prompts += [key]
        else:
            prompts += [','.join(collect_prompt(prompt_dict, build_search_keys(key), exclude_keys=loaded_keys))]

    prompt = ','.join(prompts).strip()
    if prompt == "":
        return (None, None)

    positive, negative = expand_prompt_tag(prompt, prompt_dict, loaded_keys, loras)
    return (positive, negative)

class TomlPromptDecode:
    RETURN_TYPES = ("STRING", "STRING", "STRING", "INT", "STRING")
    OUTPUT_TOOLTIPS = ("Positive prompt", "Negative prompt", "Loaded LoRA name list", "Random seed", "Summary")
    FUNCTION = "load_prompt"
    CATEGORY = "utils"
    DESCRIPTION = "Load toml prompt."

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "key_name_list": ("STRING", {"multiline": True, "dynamicPrompts": True, "tooltip": "Select Key Name"}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff, "tooltip": "Random seed."}),
                "text": ("STRING", {"multiline": True, "dynamicPrompts": True, "defaultInput": True, "tooltip": "TOML format prompt."}),
            }
        }

    def __init__(self):
        self.loras = []
        self.positive = []
        self.negative = []
        self.loaded_keys = []

    def load_prompt(self, seed, text, key_name_list):
        random.seed(seed)
        self.loras = []
        self.positive = []
        self.negative = []
        self.loaded_keys = []

        prompt_dict = tomllib.loads(text)
        key_name_list = select_dynamic_prompt(remove_comment_out(key_name_list))
        for key_str in key_name_list.splitlines():
            key_str = key_str.strip()
            if key_str == "":
                continue

            positive, negative = load_prompt_line(key_str, prompt_dict, self.loaded_keys, self.loras)
            self.positive += [positive]
            self.negative += [negative]

        positive = ",\n".join([v for v in self.positive if v])
        negative = ",\n".join([v for v in self.negative if v])
        lora_list = "\n".join(self.loras)
        summary = f"---- Positive ----\n{positive}\n\n---- Negative ----\n{negative}\n\n---- LoRA ----\n{lora_list}\n\n---- Seed ----\n{seed}"
        return (positive, negative, lora_list, seed, summary)
