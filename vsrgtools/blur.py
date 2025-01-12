from __future__ import annotations

from functools import partial
from itertools import count
from typing import Any, Literal

from vsexprtools import ExprList, ExprOp, ExprVars, complexpr_available, norm_expr
from vskernels import Gaussian
from vstools import (
    ConvMode, CustomIndexError, FunctionUtil, PlanesT, StrList, check_variable, core, depth,
    get_depth, join, normalize_planes, split, to_arr, vs
)

from .enum import BlurMatrix, BlurMatrixBase, LimitFilterMode
from .limit import limit_filter
from .util import normalize_radius

__all__ = [
    'box_blur', 'side_box_blur',
    'gauss_blur',
    'min_blur', 'sbr', 'median_blur',
    'bilateral', 'flux_smooth'
]


def box_blur(
    clip: vs.VideoNode, radius: int | list[int] = 1, passes: int = 1,
    mode: Literal[ConvMode.HV] | Literal[ConvMode.HORIZONTAL] | Literal[ConvMode.VERTICAL] = ConvMode.HV,
    planes: PlanesT = None
) -> vs.VideoNode:
    assert check_variable(clip, box_blur)

    planes = normalize_planes(clip, planes)

    if isinstance(radius, list):
        return normalize_radius(clip, box_blur, radius, planes, passes=passes)

    if not radius:
        return clip

    box_args = (
        planes,
        radius, 0 if mode == ConvMode.VERTICAL else passes,
        radius, 0 if mode == ConvMode.HORIZONTAL else passes
    )

    if hasattr(core, 'vszip'):
        blurred = clip.vszip.BoxBlur(*box_args)
    else:
        if radius > 12 and not (clip.format.sample_type == vs.FLOAT and clip.format.bits_per_sample == 16):
            blurred = clip.std.BoxBlur(*box_args)
        else:
            blurred = BlurMatrix.MEAN(radius, mode=mode)(clip, planes, passes=passes)

    return blurred


def side_box_blur(
    clip: vs.VideoNode, radius: int | list[int] = 1, planes: PlanesT = None,
    inverse: bool = False
) -> vs.VideoNode:
    planes = normalize_planes(clip, planes)

    if isinstance(radius, list):
        return normalize_radius(clip, side_box_blur, radius, planes, inverse=inverse)

    half_kernel = [(1 if i <= 0 else 0) for i in range(-radius, radius + 1)]

    conv_m1 = partial(core.std.Convolution, matrix=half_kernel, planes=planes)
    conv_m2 = partial(core.std.Convolution, matrix=half_kernel[::-1], planes=planes)
    blur_pt = partial(box_blur, planes=planes)

    vrt_filters, hrz_filters = [
        [
            partial(conv_m1, mode=mode), partial(conv_m2, mode=mode),
            partial(blur_pt, hradius=hr, vradius=vr, hpasses=h, vpasses=v)
        ] for h, hr, v, vr, mode in [
            (0, None, 1, radius, ConvMode.VERTICAL), (1, radius, 0, None, ConvMode.HORIZONTAL)
        ]
    ]

    vrt_intermediates = (vrt_flt(clip) for vrt_flt in vrt_filters)
    intermediates = list(
        hrz_flt(vrt_intermediate)
        for i, vrt_intermediate in enumerate(vrt_intermediates)
        for j, hrz_flt in enumerate(hrz_filters) if not i == j == 2
    )

    comp_blur = None if inverse else box_blur(clip, radius, 1, planes=planes)

    if complexpr_available:
        template = '{cum} x - abs {new} x - abs < {cum} {new} ?'

        cum_expr, cumc = '', 'y'
        n_inter = len(intermediates)

        for i, newc, var in zip(count(), ExprVars[2:26], ExprVars[4:26]):
            if i == n_inter - 1:
                break

            cum_expr += template.format(cum=cumc, new=newc)

            if i != n_inter - 2:
                cumc = var.upper()
                cum_expr += f' {cumc}! '
                cumc = f'{cumc}@'

        if comp_blur:
            clips = [clip, *intermediates, comp_blur]
            cum_expr = f'x {cum_expr} - {ExprVars[n_inter + 1]} +'
        else:
            clips = [clip, *intermediates]

        cum = norm_expr(clips, cum_expr, planes, force_akarin='vsrgtools.side_box_blur')
    else:
        cum = intermediates[0]
        for new in intermediates[1:]:
            cum = limit_filter(clip, cum, new, LimitFilterMode.SIMPLE2_MIN, planes)

        if comp_blur:
            cum = clip.std.MakeDiff(cum).std.MergeDiff(comp_blur)

    if comp_blur:
        return box_blur(cum, 1, min(radius // 2, 1))

    return cum


def gauss_blur(
    clip: vs.VideoNode, sigma: float | list[float] = 0.5, taps: int | None = None,
    mode: ConvMode = ConvMode.HV, planes: PlanesT = None
) -> vs.VideoNode:
    assert check_variable(clip, gauss_blur)

    planes = normalize_planes(clip, planes)

    if isinstance(sigma, list):
        return normalize_radius(clip, gauss_blur, ('sigma', sigma), planes, mode=mode)

    if mode in ConvMode.VERTICAL:
        sigma = min(sigma, clip.height)

    if mode in ConvMode.HORIZONTAL:
        sigma = min(sigma, clip.width)

    taps = BlurMatrix.GAUSS.get_taps(sigma, taps)

    no_resize2 = not hasattr(core, 'resize2')

    kernel: BlurMatrixBase[float] = BlurMatrix.GAUSS(  # type: ignore
        taps, sigma=sigma, mode=mode, scale_value=1.0 if no_resize2 and taps > 12 else 1023
    )

    if len(kernel) <= 25:
        return kernel(clip, planes)

    if no_resize2:
        if not complexpr_available:
            raise CustomRuntimeError(
                'With a high sigma you need a high number of taps, '
                'and that\'t only supported with vskernels scaling or akarin expr!'
                '\nInstall one of the two plugins (resize2, akarin) or set a lower number of taps (<= 12)!'
            )

        proc: vs.VideoNode = clip

        if ConvMode.HORIZONTAL in mode:
            proc = ExprOp.convolution('x', kernel, mode=ConvMode.HORIZONTAL)(proc)

        if ConvMode.VERTICAL in mode:
            proc = ExprOp.convolution('x', kernel, mode=ConvMode.VERTICAL)(proc)

        return proc

    def _resize2_blur(plane: vs.VideoNode) -> vs.VideoNode:
        return Gaussian(sigma, taps).scale(plane, **{f'force_{k}': k in mode for k in 'hv'})  # type: ignore

    if not {*range(clip.format.num_planes)} - {*planes}:
        return _resize2_blur(clip)

    return join([
        _resize2_blur(p) if i in planes else p
        for i, p in enumerate(split(clip))
    ])


def min_blur(
        clip: vs.VideoNode, radius: int | list[int] = 1,
        mode: ConvMode = ConvMode.HV, planes: PlanesT = None
) -> vs.VideoNode:
    """
    MinBlur by Didée (http://avisynth.nl/index.php/MinBlur)
    Nifty Gauss/Median combination
    """
    assert check_variable(clip, min_blur)

    planes = normalize_planes(clip, planes)

    if isinstance(radius, list):
        return normalize_radius(clip, min_blur, radius, planes)

    blurred = BlurMatrix.BINOMIAL(radius=radius, mode=mode)(clip, planes=planes)

    median = median_blur(clip, radius, mode, planes=planes)

    return norm_expr(
        [clip, blurred, median],
        'x y - D1! x z - D2! D1@ D2@ xor x D1@ abs D2@ abs < y z ? ?',
        planes=planes
    )


def sbr(
    clip: vs.VideoNode, radius: int | list[int] = 1,
    mode: ConvMode = ConvMode.HV, planes: PlanesT = None
) -> vs.VideoNode:
    assert check_variable(clip, sbr)

    planes = normalize_planes(clip, planes)

    blur_kernel = BlurMatrix.BINOMIAL(radius=radius, mode=mode)

    blurred = blur_kernel(clip, planes=planes)

    diff = clip.std.MakeDiff(blurred, planes=planes)
    blurred_diff = blur_kernel(diff, planes=planes)

    limited_diff = norm_expr(
        [diff, blurred_diff],
        'x y - D1! x neutral - D2! D1@ D2@ xor neutral D1@ abs D2@ abs < D1@ neutral + x ? ?',
        planes=planes
    )

    return clip.std.MakeDiff(limited_diff, planes=planes)


def median_blur(
    clip: vs.VideoNode, radius: int | list[int] = 1, mode: ConvMode = ConvMode.HV, planes: PlanesT = None
) -> vs.VideoNode:
    if radius == 1 and mode in (ConvMode.HV, ConvMode.SQUARE):
        return clip.std.Median(planes=planes)

    def _get_vals(radius: int) -> tuple[StrList, int, int, int]:
        matrix = ExprOp.matrix('x', radius, mode, [(0, 0)])
        rb = len(matrix) + 1
        st = rb - 1
        sp = rb // 2 - 1
        dp = st - 2

        return matrix, st, sp, dp

    return norm_expr(clip, (
        f"{matrix} sort{st} swap{sp} min! swap{sp} max! drop{dp} x min@ max@ clip"
        for matrix, st, sp, dp in map(_get_vals, to_arr(radius))
    ), planes, force_akarin=median_blur)


def bilateral(
    clip: vs.VideoNode, sigmaS: float | list[float] = 3.0, sigmaR: float | list[float] = 0.02,
    ref: vs.VideoNode | None = None, radius: int | list[int] | None = None,
    device_id: int = 0, num_streams: int | None = None, use_shared_memory: bool = True,
    block_x: int | None = None, block_y: int | None = None, planes: PlanesT = None,
    *, gpu: bool | None = None
) -> vs.VideoNode:
    func = FunctionUtil(clip, bilateral, planes)

    sigmaS, sigmaR = func.norm_seq(sigmaS), func.norm_seq(sigmaR)

    if gpu is not False:
        basic_args, new_args = (sigmaS, sigmaR, radius, device_id), (num_streams, use_shared_memory)

        if hasattr(core, 'bilateralgpu_rtc'):
            return clip.bilateralgpu_rtc.Bilateral(*basic_args, *new_args, block_x, block_y, ref)
        else:
            return clip.bilateralgpu.Bilateral(*basic_args, *new_args, ref)

    if (bits := get_depth(clip)) > 16:
        clip = depth(clip, 16)

    if ref and clip.format != ref.format:
        ref = depth(ref, clip)

    clip = clip.vszip.Bilateral(ref, sigmaS, sigmaR)

    return depth(clip, bits)


def flux_smooth(
    clip: vs.VideoNode, radius: int = 2, threshold: int = 7, scenechange: int = 24, planes: PlanesT = None
) -> vs.VideoNode:
    assert check_variable(clip, flux_smooth)

    if radius < 1 or radius > 7:
        raise CustomIndexError('Radius must be between 1 and 7 (inclusive)!', flux_smooth, reason=radius)

    planes = normalize_planes(clip, planes)

    threshold = threshold << clip.format.bits_per_sample - 8

    cthreshold = threshold if (1 in planes or 2 in planes) else 0

    median = clip.tmedian.TemporalMedian(radius, planes)  # type: ignore
    average = clip.focus2.TemporalSoften2(  # type: ignore
        radius, threshold, cthreshold, scenechange
    )

    return limit_filter(average, clip, median, LimitFilterMode.DIFF_MIN, planes)
