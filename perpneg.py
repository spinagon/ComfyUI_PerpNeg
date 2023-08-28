import torch
import math
import comfy.model_management
import comfy.sample
import comfy.samplers
import comfy.utils
import latent_preview


def lcm(a, b):  # TODO: eventually replace by math.lcm (added in python3.9)
    return abs(a*b) // math.gcd(a, b)


def common_ksampler(model, seed, steps, cfg, sampler_name, scheduler, positive, negative, latent, denoise=1.0, disable_noise=False, start_step=None, last_step=None, force_full_denoise=False):
    device = comfy.model_management.get_torch_device()
    latent_image = latent["samples"]

    if disable_noise:
        noise = torch.zeros(latent_image.size(
        ), dtype=latent_image.dtype, layout=latent_image.layout, device="cpu")
    else:
        batch_inds = latent["batch_index"] if "batch_index" in latent else None
        noise = comfy.sample.prepare_noise(latent_image, seed, batch_inds)

    noise_mask = None
    if "noise_mask" in latent:
        noise_mask = latent["noise_mask"]

    preview_format = "JPEG"
    if preview_format not in ["JPEG", "PNG"]:
        preview_format = "JPEG"

    previewer = latent_preview.get_previewer(device, model.model.latent_format)

    pbar = comfy.utils.ProgressBar(steps)

    def callback(step, x0, x, total_steps):
        preview_bytes = None
        if previewer:
            preview_bytes = previewer.decode_latent_to_preview_image(
                preview_format, x0)
        pbar.update_absolute(step + 1, total_steps, preview_bytes)

    samples = comfy.sample.sample(model, noise, steps, cfg, sampler_name, scheduler, positive, negative, latent_image,
                                  denoise=denoise, disable_noise=disable_noise, start_step=start_step, last_step=last_step,
                                  force_full_denoise=force_full_denoise, noise_mask=noise_mask, callback=callback, seed=seed)
    out = latent.copy()
    out["samples"] = samples
    return (out, )


class KSamplerAdvancedPerpNeg:
    @classmethod
    def INPUT_TYPES(s):
        return {"required":
                {"model": ("MODEL", ),
                 "clip": ("CLIP", ),
                 "add_noise": (["enable", "disable"], ),
                 "noise_seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                 "steps": ("INT", {"default": 20, "min": 1, "max": 10000}),
                 "cfg": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 100.0}),
                 "sampler_name": (comfy.samplers.KSampler.SAMPLERS, ),
                 "scheduler": (comfy.samplers.KSampler.SCHEDULERS, ),
                 "positive": ("CONDITIONING", ),
                 "negative": ("CONDITIONING", ),
                 "latent_image": ("LATENT", ),
                 "start_at_step": ("INT", {"default": 0, "min": 0, "max": 10000}),
                 "end_at_step": ("INT", {"default": 10000, "min": 0, "max": 10000}),
                 "return_with_leftover_noise": (["disable", "enable"], ),
                 }
                }

    RETURN_TYPES = ("LATENT",)
    FUNCTION = "sample"

    CATEGORY = "sampling"

    def sample(self, model, clip, add_noise, noise_seed, steps, cfg, sampler_name, scheduler, positive, negative, latent_image, start_at_step, end_at_step, return_with_leftover_noise, denoise=1.0):
        force_full_denoise = True
        if return_with_leftover_noise == "enable":
            force_full_denoise = False
        disable_noise = False
        if add_noise == "disable":
            disable_noise = True

        tokens = clip.tokenize("")
        uncond_proper, uncond_proper_pooled = clip.encode_from_tokens(tokens, return_pooled=True)
        uncond_proper = [[uncond_proper, {"pooled_output": uncond_proper_pooled}]]

        def sampling_function_perpneg(model_function, x, timestep, uncond, cond, cond_scale, cond_concat=[], model_options={}, seed=None):
            def get_area_and_mult(cond, x_in, cond_concat_in, timestep_in):
                area = (x_in.shape[2], x_in.shape[3], 0, 0)
                strength = 1.0
                if 'timestep_start' in cond[1]:
                    timestep_start = cond[1]['timestep_start']
                    if timestep_in[0] > timestep_start:
                        return None
                if 'timestep_end' in cond[1]:
                    timestep_end = cond[1]['timestep_end']
                    if timestep_in[0] < timestep_end:
                        return None
                if 'area' in cond[1]:
                    area = cond[1]['area']
                if 'strength' in cond[1]:
                    strength = cond[1]['strength']

                adm_cond = None
                if 'adm_encoded' in cond[1]:
                    adm_cond = cond[1]['adm_encoded']

                input_x = x_in[:, :, area[2]:area[0] +
                               area[2], area[3]:area[1] + area[3]]
                if 'mask' in cond[1]:
                    # Scale the mask to the size of the input
                    # The mask should have been resized as we began the sampling process
                    mask_strength = 1.0
                    if "mask_strength" in cond[1]:
                        mask_strength = cond[1]["mask_strength"]
                    mask = cond[1]['mask']
                    assert (mask.shape[1] == x_in.shape[2])
                    assert (mask.shape[2] == x_in.shape[3])
                    mask = mask[:, area[2]:area[0] + area[2],
                                area[3]:area[1] + area[3]] * mask_strength
                    mask = mask.unsqueeze(1).repeat(
                        input_x.shape[0] // mask.shape[0], input_x.shape[1], 1, 1)
                else:
                    mask = torch.ones_like(input_x)
                mult = mask * strength

                if 'mask' not in cond[1]:
                    rr = 8
                    if area[2] != 0:
                        for t in range(rr):
                            mult[:, :, t:1+t, :] *= ((1.0/rr) * (t + 1))
                    if (area[0] + area[2]) < x_in.shape[2]:
                        for t in range(rr):
                            mult[:, :, area[0] - 1 - t:area[0] -
                                 t, :] *= ((1.0/rr) * (t + 1))
                    if area[3] != 0:
                        for t in range(rr):
                            mult[:, :, :, t:1+t] *= ((1.0/rr) * (t + 1))
                    if (area[1] + area[3]) < x_in.shape[3]:
                        for t in range(rr):
                            mult[:, :, :, area[1] - 1 - t:area[1] -
                                 t] *= ((1.0/rr) * (t + 1))

                conditionning = {}
                conditionning['c_crossattn'] = cond[0]
                if cond_concat_in is not None and len(cond_concat_in) > 0:
                    cropped = []
                    for x in cond_concat_in:
                        cr = x[:, :, area[2]:area[0] +
                               area[2], area[3]:area[1] + area[3]]
                        cropped.append(cr)
                    conditionning['c_concat'] = torch.cat(cropped, dim=1)

                if adm_cond is not None:
                    conditionning['c_adm'] = adm_cond

                control = None
                if 'control' in cond[1]:
                    control = cond[1]['control']

                patches = None
                if 'gligen' in cond[1]:
                    gligen = cond[1]['gligen']
                    patches = {}
                    gligen_type = gligen[0]
                    gligen_model = gligen[1]
                    if gligen_type == "position":
                        gligen_patch = gligen_model.model.set_position(
                            input_x.shape, gligen[2], input_x.device)
                    else:
                        gligen_patch = gligen_model.model.set_empty(
                            input_x.shape, input_x.device)

                    patches['middle_patch'] = [gligen_patch]

                return (input_x, mult, conditionning, area, control, patches)

            def cond_equal_size(c1, c2):
                if c1 is c2:
                    return True
                if c1.keys() != c2.keys():
                    return False
                if 'c_crossattn' in c1:
                    s1 = c1['c_crossattn'].shape
                    s2 = c2['c_crossattn'].shape
                    if s1 != s2:
                        if s1[0] != s2[0] or s1[2] != s2[2]:  # these 2 cases should not happen
                            return False

                        mult_min = lcm(s1[1], s2[1])
                        diff = mult_min // min(s1[1], s2[1])
                        if diff > 4:  # arbitrary limit on the padding because it's probably going to impact performance negatively if it's too much
                            return False
                if 'c_concat' in c1:
                    if c1['c_concat'].shape != c2['c_concat'].shape:
                        return False
                if 'c_adm' in c1:
                    if c1['c_adm'].shape != c2['c_adm'].shape:
                        return False
                return True

            def can_concat_cond(c1, c2):
                if c1[0].shape != c2[0].shape:
                    return False

                # control
                if (c1[4] is None) != (c2[4] is None):
                    return False
                if c1[4] is not None:
                    if c1[4] is not c2[4]:
                        return False

                # patches
                if (c1[5] is None) != (c2[5] is None):
                    return False
                if (c1[5] is not None):
                    if c1[5] is not c2[5]:
                        return False

                return cond_equal_size(c1[2], c2[2])

            def cond_cat(c_list):
                c_crossattn = []
                c_concat = []
                c_adm = []
                crossattn_max_len = 0
                for x in c_list:
                    if 'c_crossattn' in x:
                        c = x['c_crossattn']
                        if crossattn_max_len == 0:
                            crossattn_max_len = c.shape[1]
                        else:
                            crossattn_max_len = lcm(
                                crossattn_max_len, c.shape[1])
                        c_crossattn.append(c)
                    if 'c_concat' in x:
                        c_concat.append(x['c_concat'])
                    if 'c_adm' in x:
                        c_adm.append(x['c_adm'])
                out = {}
                c_crossattn_out = []
                for c in c_crossattn:
                    if c.shape[1] < crossattn_max_len:
                        # padding with repeat doesn't change result
                        c = c.repeat(1, crossattn_max_len // c.shape[1], 1)
                    c_crossattn_out.append(c)

                if len(c_crossattn_out) > 0:
                    out['c_crossattn'] = [torch.cat(c_crossattn_out)]
                if len(c_concat) > 0:
                    out['c_concat'] = [torch.cat(c_concat)]
                if len(c_adm) > 0:
                    out['c_adm'] = torch.cat(c_adm)
                return out

            def calc_cond_uncond_batch(model_function, cond, uncond, x_in, timestep, max_total_area, cond_concat_in, model_options):
                out_cond = torch.zeros_like(x_in)
                out_count = torch.ones_like(x_in)/100000.0

                out_uncond = torch.zeros_like(x_in)
                out_uncond_count = torch.ones_like(x_in)/100000.0

                COND = 0
                UNCOND = 1

                to_run = []
                for x in cond:
                    p = get_area_and_mult(x, x_in, cond_concat_in, timestep)
                    if p is None:
                        continue

                    to_run += [(p, COND)]
                if uncond is not None:
                    for x in uncond:
                        p = get_area_and_mult(
                            x, x_in, cond_concat_in, timestep)
                        if p is None:
                            continue

                        to_run += [(p, UNCOND)]

                while len(to_run) > 0:
                    first = to_run[0]
                    first_shape = first[0][0].shape
                    to_batch_temp = []
                    for x in range(len(to_run)):
                        if can_concat_cond(to_run[x][0], first[0]):
                            to_batch_temp += [x]

                    to_batch_temp.reverse()
                    to_batch = to_batch_temp[:1]

                    for i in range(1, len(to_batch_temp) + 1):
                        batch_amount = to_batch_temp[:len(to_batch_temp)//i]
                        if (len(batch_amount) * first_shape[0] * first_shape[2] * first_shape[3] < max_total_area):
                            to_batch = batch_amount
                            break

                    input_x = []
                    mult = []
                    c = []
                    cond_or_uncond = []
                    area = []
                    control = None
                    patches = None
                    for x in to_batch:
                        o = to_run.pop(x)
                        p = o[0]
                        input_x += [p[0]]
                        mult += [p[1]]
                        c += [p[2]]
                        area += [p[3]]
                        cond_or_uncond += [o[1]]
                        control = p[4]
                        patches = p[5]

                    batch_chunks = len(cond_or_uncond)
                    input_x = torch.cat(input_x)
                    c = cond_cat(c)
                    timestep_ = torch.cat([timestep] * batch_chunks)

                    if control is not None:
                        c['control'] = control.get_control(
                            input_x, timestep_, c, len(cond_or_uncond))

                    transformer_options = {}
                    if 'transformer_options' in model_options:
                        transformer_options = model_options['transformer_options'].copy(
                        )

                    if patches is not None:
                        if "patches" in transformer_options:
                            cur_patches = transformer_options["patches"].copy()
                            for p in patches:
                                if p in cur_patches:
                                    cur_patches[p] = cur_patches[p] + \
                                        patches[p]
                                else:
                                    cur_patches[p] = patches[p]
                        else:
                            transformer_options["patches"] = patches

                    c['transformer_options'] = transformer_options

                    if 'model_function_wrapper' in model_options:
                        output = model_options['model_function_wrapper'](model_function, {
                            "input": input_x, "timestep": timestep_, "c": c, "cond_or_uncond": cond_or_uncond}).chunk(batch_chunks)
                    else:
                        output = model_function(
                            input_x, timestep_, **c).chunk(batch_chunks)
                    del input_x

                    comfy.model_management.throw_exception_if_processing_interrupted()

                    for o in range(batch_chunks):
                        if cond_or_uncond[o] == COND:
                            out_cond[:, :, area[o][2]:area[o][0] + area[o][2], area[o]
                                     [3]:area[o][1] + area[o][3]] += output[o] * mult[o]
                            out_count[:, :, area[o][2]:area[o][0] + area[o][2],
                                      area[o][3]:area[o][1] + area[o][3]] += mult[o]
                        else:
                            out_uncond[:, :, area[o][2]:area[o][0] + area[o][2],
                                       area[o][3]:area[o][1] + area[o][3]] += output[o] * mult[o]
                            out_uncond_count[:, :, area[o][2]:area[o][0] + area[o]
                                             [2], area[o][3]:area[o][1] + area[o][3]] += mult[o]
                    del mult

                out_cond /= out_count
                del out_count
                out_uncond /= out_uncond_count
                del out_uncond_count

                return out_cond, out_uncond

            max_total_area = comfy.model_management.maximum_batch_area()
            if math.isclose(cond_scale, 1.0):
                uncond = None

            # cond, uncond = calc_cond_uncond_batch(
            #     model_function, cond, uncond, x, timestep, max_total_area, cond_concat, model_options)
            # if "sampler_cfg_function" in model_options:
            #     args = {"cond": cond, "uncond": uncond,
            #             "cond_scale": cond_scale, "timestep": timestep}
            #     return model_options["sampler_cfg_function"](args)
            # else:
            #     return uncond + (cond - uncond) * cond_scale
            noise_pred_pos, noise_pred_neg = calc_cond_uncond_batch(
                model_function, cond, uncond, x, timestep, max_total_area, cond_concat, model_options)
            noise_pred_uncond, _ = calc_cond_uncond_batch(
                model_function, uncond_proper, uncond_proper, x, timestep, max_total_area, cond_concat, model_options)
            print([noise_pred_pos.shape, noise_pred_neg.shape, noise_pred_uncond.shape])
            return noise_pred_neg + (noise_pred_pos - noise_pred_neg) * cond_scale
            
        comfy.samplers.sampling_function = sampling_function_perpneg
        
        return common_ksampler(model, noise_seed, steps, cfg, sampler_name, scheduler, positive, negative, latent_image, denoise=denoise, disable_noise=disable_noise, start_step=start_at_step, last_step=end_at_step, force_full_denoise=force_full_denoise)


NODE_CLASS_MAPPINGS = {
    "KSamplerAdvancedPerpNeg": KSamplerAdvancedPerpNeg,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "KSamplerAdvancedPerpNeg": "KSampler (Advanced + Perp-Neg)",
}