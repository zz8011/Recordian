"""录音 overlay 的液态玻璃球 shader。

从 https://github.com/LerSent001/orb （MIT License）的 ``effect.wgsl`` 移植，
只保留「声纹薄膜 voiceWave」（style 19）预设及其玻璃外壳。原项目为
WebGPU/WGSL，这里翻译为 GLSL 330 供 pyglet 使用。

相对原版的改动：

- 丢弃 voiceWave 用不到的代码：fbm 噪声库、12 段调色板 ramp、其余 10 个
  预设流体、legacy progA/progB 流体、metal 系参数。
- ``glassEnabled`` 固定为开，``style`` 固定 19（轮廓强度常量 0.11）。
- 原版球外返回不透明黑；Recordian 是透明 overlay 窗口，球外改为 alpha 0
  （边缘光晕按亮度给 alpha）。
- 新增 ``u_audio``（0..1 平滑音量）驱动膜振幅、波峰亮度和边缘光晕。
"""

from __future__ import annotations

FRAGMENT_SRC = """
#version 330 core
in vec2 v_uv;
out vec4 fragColor;

uniform vec2 u_size;
uniform float u_time;           // 秒，CPU 侧已按状态缩放
uniform float u_audio;          // 0..1 平滑音量（仅录音时非零）
uniform float u_radius;
uniform float u_zoom;
uniform float u_warp;
uniform float u_ridgeAmt;
uniform float u_shade;
uniform float u_sheen;
uniform float u_gloss;
uniform float u_shellMidAlpha;
uniform float u_shellEdgeAlpha;
uniform float u_exposure;
uniform float u_edgeSoftness;
uniform float u_edgeGlow;
uniform float u_glassOpacity;
uniform float u_contourDeform;
uniform vec3 u_colorA;
uniform vec3 u_colorB;
uniform vec3 u_colorC;
uniform vec3 u_colorD;
uniform vec3 u_highlightColor;
uniform vec3 u_shellInner;
uniform vec3 u_shellMid;
uniform vec3 u_shellEdge;
uniform vec3 u_sheenColor;
uniform vec3 u_specColor;
uniform vec3 u_canvasColor;
uniform vec3 u_glowColor;

// Pure fluid reaches the ball edge.
const float GL_CLEAR_EA = 0.995;
const float GL_CLEAR_EB = 1.04;
// style >= 18.5 的轮廓强度（voiceWave 固定 0.11）
const float CONTOUR_STRENGTH = 0.11;

// ── The Orbs edge bank ─────────────────────────────────────────────────────

float mfEdgeD(float soft) {
    return soft - 0.005;
}

// The halo an orb throws past its own limb. ADDED, never subtracted.
vec3 mfEdgeGlow(vec3 col, vec2 uv, vec2 ctr, float rad,
                float soft, float glow, vec3 glowRGB) {
    if (glow <= 0.0) { return col; }
    float r = length(uv - ctr);
    float outside = smoothstep(rad - max(soft, 0.0005), rad + max(soft, 0.0005), r);
    return col + glowRGB * (glow * exp(-max(r - rad, 0.0) * 11.0) * outside);
}

// ── voiceWave fluid ─────────────────────────────────────────────────────────

vec3 glsFinishPresetFluid(vec3 colorIn, vec2 p) {
    vec3 color = colorIn;
    color = mix(color, u_highlightColor,
                u_shade * 0.22 * smoothstep(0.15, 1.15, dot(p, vec2(-0.32, 0.78))));
    color = color * (1.0 - u_shade * 0.34
                    * smoothstep(-0.1, 1.2, dot(p, vec2(0.45, -0.62))));
    color = color * (1.0 - u_shade * 0.22 * smoothstep(0.72, 1.08, length(p)));
    return clamp(color, vec3(0.0), vec3(1.0));
}

vec3 glsVoiceWaveFluid(vec2 p, float t) {
    // A single broad membrane stays phase-coherent across the sphere. Nearby
    // translucent layers add volume without splitting into separate Siri bands.
    float scale = 0.76 + u_zoom * 0.34;
    vec2 q = p / scale;
    float rimEnvelope = pow(max(1.0 - q.x * q.x, 0.0), 0.72);
    float drift = t * 0.82;
    // u_audio：录音时膜随音量起伏
    float amplitude = (0.2 + u_warp * 0.018) * (1.0 + u_audio * 2.2);
    float mainY = rimEnvelope * (amplitude * sin(q.x * 1.48 + drift)
                  + 0.055 * sin(q.x * 3.2 - drift * 0.43 + 1.1));
    float distance = q.y - mainY;
    float width = (0.11 + (1.0 - u_ridgeAmt) * 0.075) * (1.0 + u_audio * 0.3);
    float membrane = exp(-distance * distance / max(width * width, 0.001)) * rimEnvelope;
    float upperVeil = exp(-(distance - 0.105) * (distance - 0.105)
                          / max(width * width * 2.4, 0.001)) * rimEnvelope;
    float lowerVeil = exp(-(distance + 0.115) * (distance + 0.115)
                          / max(width * width * 2.8, 0.001)) * rimEnvelope;
    float crest = exp(-distance * distance / 0.0026) * rimEnvelope;
    float depth = sqrt(max(1.0 - clamp(dot(p, p), 0.0, 1.0), 0.0));
    vec3 color = mix(u_colorA * 0.7, u_colorD * 0.34,
                     smoothstep(-0.82, 0.82, q.y));
    color = mix(color, u_colorB, upperVeil * 0.7);
    color = mix(color, u_colorC, lowerVeil * 0.62);
    color = color + mix(u_colorB, u_colorC, 0.46) * membrane * 0.34;
    // u_audio：波峰随音量增亮
    color = color + u_highlightColor * crest * (0.14 + u_audio * 0.7);
    color = color * (0.58 + 0.42 * depth);
    return glsFinishPresetFluid(color, p);
}

// ── The shell ───────────────────────────────────────────────────────────────

// Source-over onto an opaque destination, straight (un-premultiplied) sRGB.
vec3 glsOver(vec3 dst, vec3 src, float a) {
    float k = clamp(a, 0.0, 1.0);
    return src * k + dst * (1.0 - k);
}

float glsRefractionProfile(float t) {
    float depth = clamp(t, 0.0, 1.0);
    float circular = sqrt(max(1.0 - (1.0 - depth) * (1.0 - depth), 0.0));
    return 1.0 - circular;
}

float glsHighlightLobe(vec2 normal, vec2 direction, float cut, float power) {
    float angular = clamp((dot(normal, direction) - cut) / max(1.0 - cut, 0.001),
                          0.0, 1.0);
    return pow(angular, power);
}

// style == 19 (voiceWave) 的轮廓波
vec2 glsContourWave(float angle, float t) {
    float wave = sin(angle * 2.0 + t * 0.27) * 0.72
                 + sin(angle * 4.0 - t * 0.16 + 2.1) * 0.28;
    float slope = cos(angle * 2.0 + t * 0.27) * 1.44
                  + cos(angle * 4.0 - t * 0.16 + 2.1) * 1.12;
    return vec2(wave, slope);
}

float glsContourScale(vec2 uv, float t, float amount) {
    if (amount <= 0.0) { return 1.0; }
    vec2 contour = glsContourWave(atan(uv.y, uv.x), t);
    return 1.0 + clamp(amount, 0.0, 1.0) * CONTOUR_STRENGTH * contour.x;
}

vec2 glsContourNormal(vec2 uv, float rad, float t, float amount) {
    float dist = length(uv);
    if (dist <= 0.0001) { return vec2(0.0); }
    vec2 radial = uv / dist;
    vec2 contour = glsContourWave(atan(uv.y, uv.x), t);
    float slope = clamp(amount, 0.0, 1.0) * CONTOUR_STRENGTH * contour.y;
    vec2 tangent = vec2(-radial.y, radial.x);
    return normalize(radial - tangent * (rad * slope / dist));
}

void main() {
    // v_uv 来自 vertex shader，y 向上（OpenGL 约定），与 orb 创作方向一致。
    vec2 fc = v_uv * u_size;
    vec2 uv = (2.0 * fc - u_size) / max(min(u_size.x, u_size.y), 1.0);

    float rad = max(u_radius, 0.05);
    float t = u_time;
    // u_audio：球体轮廓随音量轻微呼吸（保持球体剪影，不做明显形变）
    float deform = u_contourDeform + u_audio * 0.05;
    float contourRad = rad * glsContourScale(uv, t, deform);
    float edgeD = mfEdgeD(u_edgeSoftness);
    // u_audio：边缘光晕随音量呼吸
    float glowAmt = u_edgeGlow + u_audio * 1.0;

    // 球外：透明，只保留边缘光晕
    if (length(uv) > contourRad * (1.01 + edgeD)) {
        vec3 g = clamp(mfEdgeGlow(vec3(0.0), uv, vec2(0.0), contourRad,
                                  u_edgeSoftness, glowAmt, u_glowColor),
                       vec3(0.0), vec3(1.0));
        fragColor = vec4(g, clamp(dot(g, vec3(1.0 / 3.0)), 0.0, 1.0));
        return;
    }

    vec2 p = uv / contourRad;     // deformed ball space: |p| == 1 on the edge
    float pd = length(p);

    float clearFa = 1.0 - smoothstep(GL_CLEAR_EA, GL_CLEAR_EB, pd);
    vec2 normal = glsContourNormal(uv, rad, t, deform);
    float edgeDepth = max(1.0 - pd, 0.0);
    float refractionWidth = 0.015 + 0.95 * clamp(u_shellMidAlpha, 0.0, 1.0);
    float refractionT = edgeDepth / max(refractionWidth, 0.001);
    float refractionProfile = pow(glsRefractionProfile(refractionT), 0.68);
    float refractionAmount = 1.6 * clamp(u_glassOpacity, 0.0, 1.0)
                             * refractionProfile;
    vec2 refractedP = p - normal * refractionAmount;

    // 玻璃开启：三次流体采样做光学色散
    vec3 fcol = vec3(0.0);
    if (clearFa > 0.0) {
        float channelSplit = 0.14 * clamp(u_gloss, 0.0, 2.0)
                             * clamp(u_glassOpacity, 0.0, 1.0)
                             * refractionProfile;
        vec3 redSample = glsVoiceWaveFluid(refractedP - normal * channelSplit, t);
        vec3 greenSample = glsVoiceWaveFluid(refractedP, t);
        vec3 blueSample = glsVoiceWaveFluid(refractedP + normal * channelSplit, t);
        fcol = vec3(redSample.r, greenSample.g, blueSample.b);
    }

    float lum = dot(fcol, vec3(0.213, 0.715, 0.072));
    vec3 clearSat = clamp(vec3(lum) + (fcol - vec3(lum)) * 1.22,
                          vec3(0.0), vec3(1.0));
    vec3 col = glsOver(u_canvasColor, clearSat, 0.99 * clearFa);

    // 玻璃外壳表面光照
    float surfaceWidth = 0.026 + 0.055 * clamp(u_shellEdgeAlpha, 0.0, 1.0);
    float surfaceBand = (1.0 - smoothstep(0.0, surfaceWidth, edgeDepth)) * clearFa;
    float opticalRim = pow(surfaceBand, 1.8);
    col = glsOver(col, u_shellInner, opticalRim * u_glassOpacity * 0.45);

    vec2 coolDirection = normalize(vec2(0.84, 0.54));
    vec2 warmDirection = normalize(vec2(-0.62, -0.78));
    float coolSplit = glsHighlightLobe(normal, coolDirection, -0.32, 1.8);
    float warmSplit = glsHighlightLobe(normal, warmDirection, -0.28, 2.0);
    float dispersion = opticalRim * clamp(u_gloss, 0.0, 2.0)
                       * (0.8 + 0.8 * u_shellEdgeAlpha);
    col = glsOver(col, u_shellMid, dispersion * coolSplit);
    col = glsOver(col, u_shellEdge, dispersion * warmSplit);

    float edgeShadow = opticalRim * (0.015 + 0.15 * u_shellEdgeAlpha)
                       * (0.15 + 0.85 * max(dot(normal, vec2(0.45, -0.89)), 0.0));
    col = col * (1.0 - edgeShadow);

    vec2 keyDirection = normalize(vec2(-0.68, 0.73));
    vec2 fillDirection = normalize(vec2(0.74, -0.67));
    float key = opticalRim * glsHighlightLobe(normal, keyDirection, 0.2, 2.8)
                * clamp(u_sheen, 0.0, 2.0) * 1.4;
    float fill = opticalRim * glsHighlightLobe(normal, fillDirection, 0.4, 3.6)
                 * clamp(u_sheen, 0.0, 2.0) * 1.0;
    col = glsOver(col, u_sheenColor, key);
    col = glsOver(col, u_specColor, fill);

    // 球体边缘覆盖
    float ballA = 1.0 - smoothstep(0.99 - edgeD, 1.01 + edgeD, pd);
    col = clamp(col * max(u_exposure, 0.0), vec3(0.0), vec3(1.0)) * ballA;
    vec3 edged = clamp(mfEdgeGlow(col, uv, vec2(0.0), contourRad,
                                  u_edgeSoftness, glowAmt, u_glowColor),
                       vec3(0.0), vec3(1.0));
    // alpha：球内按覆盖（近乎不透明，canvas 是深色的玻璃体），球外光晕按增量亮度
    float glowExtra = max(dot(edged - col, vec3(1.0 / 3.0)), 0.0);
    fragColor = vec4(edged, clamp(ballA * 0.99 + glowExtra, 0.0, 1.0));
}
"""

# ── voiceWave 预设参数（照抄 orb 项目 src/presets.ts，MIT License）──────────

SCALAR_UNIFORMS: dict[str, float] = {
    "u_radius": 0.7,
    "u_zoom": 0.36,
    "u_warp": 2.6,
    "u_ridgeAmt": 0.46,
    "u_shade": 0.08,
    "u_sheen": 0.22,
    "u_gloss": 0.36,
    "u_shellMidAlpha": 0.18,
    "u_shellEdgeAlpha": 0.2,
    "u_exposure": 1.35,
    "u_edgeSoftness": 0.005,
    "u_edgeGlow": 0.0,
    "u_glassOpacity": 0.48,
    "u_contourDeform": 0.1,
}

RECORDING_COLORS: dict[str, str] = {
    "u_colorA": "#09030E",
    "u_colorB": "#CE2CCB",
    "u_colorC": "#FF5C71",
    "u_colorD": "#7B53FF",
    "u_highlightColor": "#FFD9F0",
    "u_shellInner": "#FFFFFF",
    "u_shellMid": "#E48BFF",
    "u_shellEdge": "#FF7890",
    "u_sheenColor": "#FFF1FA",
    "u_specColor": "#E7D9FF",
    "u_canvasColor": "#020105",
    "u_glowColor": "#CE2CCB",
}

# 状态色覆盖：语义沿用旧 shader（处理中偏冷蓝，错误偏红橙）
STATE_COLOR_OVERRIDES: dict[str, dict[str, str]] = {
    "processing": {
        "u_colorB": "#32A8FF",
        "u_colorC": "#66E8FF",
        "u_colorD": "#1677FF",
        "u_highlightColor": "#DFF4FF",
        "u_shellMid": "#66E8FF",
        "u_shellEdge": "#32A8FF",
        "u_specColor": "#D9F3FF",
        "u_glowColor": "#1677FF",
    },
    "error": {
        "u_colorB": "#FF4D3D",
        "u_colorC": "#FF8A3D",
        "u_colorD": "#D92632",
        "u_highlightColor": "#FFE0D6",
        "u_shellMid": "#FF8A66",
        "u_shellEdge": "#FF5540",
        "u_specColor": "#FFD9CF",
        "u_glowColor": "#FF4D3D",
    },
}

# 各状态的时间流速（动画连续积分，切换不跳变）。recording 为预设 speed 0.95。
STATE_TIME_SCALE: dict[str, float] = {
    "idle": 0.95,
    "recording": 0.95,
    "processing": 1.9,
    "error": 0.65,
}


def hex_to_rgb(value: str) -> tuple[float, float, float]:
    """``#RRGGBB`` → 0..1 浮点三元组。"""
    v = value.lstrip("#")
    return (
        int(v[0:2], 16) / 255,
        int(v[2:4], 16) / 255,
        int(v[4:6], 16) / 255,
    )


def apply_orb_uniforms(program: object, state: str) -> float:
    """按状态设置球体的全部静态 uniform，返回该状态的时间流速。

    颜色以外的标量参数所有状态一致；颜色按状态覆盖。每帧只需另设
    ``u_size`` / ``u_time`` / ``u_audio``。
    """
    for name, value in SCALAR_UNIFORMS.items():
        program[name] = value  # type: ignore[index]
    colors = dict(RECORDING_COLORS)
    colors.update(STATE_COLOR_OVERRIDES.get(state, {}))
    for color_name, color_hex in colors.items():
        program[color_name] = hex_to_rgb(color_hex)  # type: ignore[index]
    return STATE_TIME_SCALE.get(state, 0.95)
