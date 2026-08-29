"""Deterministic local image OCR extraction boundary for AturUang.

Provides typed OCR extraction interfaces and a local Windows WinRT OCR transport
with deterministic vertical slicing for tall mobile screenshots.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Sequence

from PIL import Image


class ImageOcrError(RuntimeError):
    """Raised when local OCR execution fails or times out."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ImageOcrLine:
    text: str = field(repr=False)
    x: int
    y: int
    width: int
    height: int
    confidence: float = 1.0


@dataclass(frozen=True)
class ImageOcrResult:
    lines: tuple[ImageOcrLine, ...] = field(repr=False)
    image_width: int
    image_height: int

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


@dataclass(frozen=True)
class ImageOcrConfig:
    slice_height: int = 2000
    slice_overlap: int = 200
    timeout_seconds: float = 30.0
    language_tag: str = "en-US"


def _extract_slice_windows_ocr(
    slice_image: Image.Image,
    *,
    config: ImageOcrConfig,
    slice_y_offset: int,
) -> list[ImageOcrLine]:
    """Runs Windows.Media.Ocr on a single image slice via PowerShell."""
    with tempfile.TemporaryDirectory(prefix="aturuang_ocr_") as tmp_dir:
        tmp_path = Path(tmp_dir) / "slice.png"
        slice_image.save(tmp_path, format="PNG")

        ps_script = f"""
Add-Type -AssemblyName System.IO.Compression.FileSystem
Add-Type -AssemblyName System.Runtime.WindowsRuntime

[Windows.Security.Cryptography.CryptographicBuffer, Windows.Security.Cryptography, ContentType = WindowsRuntime] | Out-Null
[Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null

$lang = [Windows.Globalization.Language]::new('{config.language_tag}')
if (-not [Windows.Media.Ocr.OcrEngine]::IsLanguageSupported($lang)) {{
    $lang = [Windows.Globalization.Language]::new('en-US')
}}
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
if ($engine -eq $null) {{
    Write-Error "ENGINE_INIT_FAILED"
    exit 1
}}

$asTaskMethods = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {{ $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.IsGenericMethod }}
$asTaskMethod = $asTaskMethods[0]

function Await-Op($asyncOp, [Type]$resType) {{
    $genericMethod = $asTaskMethod.MakeGenericMethod($resType)
    $task = $genericMethod.Invoke($null, @($asyncOp))
    $task.Wait()
    return $task.Result
}}

$filePath = '{str(tmp_path)}'
$storageFile = Await-Op ([Windows.Storage.StorageFile]::GetFileFromPathAsync($filePath)) ([Windows.Storage.StorageFile])
$stream = Await-Op ($storageFile.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await-Op ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$softwareBitmap = Await-Op ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$ocrResult = Await-Op ($engine.RecognizeAsync($softwareBitmap)) ([Windows.Media.Ocr.OcrResult])

$lines = @()
foreach ($line in $ocrResult.Lines) {{
    $minX = 999999
    $minY = 999999
    $maxX = 0
    $maxY = 0
    foreach ($w in $line.Words) {{
        $rect = $w.BoundingRect
        if ($rect.X -lt $minX) {{ $minX = [int]$rect.X }}
        if ($rect.Y -lt $minY) {{ $minY = [int]$rect.Y }}
        if (($rect.X + $rect.Width) -gt $maxX) {{ $maxX = [int]($rect.X + $rect.Width) }}
        if (($rect.Y + $rect.Height) -gt $maxY) {{ $maxY = [int]($rect.Y + $rect.Height) }}
    }}
    $width = if ($maxX -gt $minX) {{ $maxX - $minX }} else {{ 0 }}
    $height = if ($maxY -gt $minY) {{ $maxY - $minY }} else {{ 0 }}
    $x = if ($minX -lt 999999) {{ $minX }} else {{ 0 }}
    $y = if ($minY -lt 999999) {{ $minY }} else {{ 0 }}

    $lines += @{{
        text = $line.Text
        x = $x
        y = $y
        width = $width
        height = $height
    }}
}}

$output = @{{
    lines = $lines
}} | ConvertTo-Json -Compress

Write-Output $output
"""
        ps_path = Path(tmp_dir) / "run_ocr.ps1"
        ps_path.write_text(ps_script, encoding="utf-8")

        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ps_path),
        ]

        flags = 0
        if sys.platform == "win32":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=config.timeout_seconds,
                creationflags=flags,
            )
        except subprocess.TimeoutExpired as exc:
            raise ImageOcrError("OCR_TIMEOUT", "Local OCR execution timed out") from exc
        except Exception as exc:
            raise ImageOcrError("OCR_SUBPROCESS_FAILED", "Failed to spawn OCR subprocess") from exc

        if proc.returncode != 0:
            raise ImageOcrError(
                "OCR_ENGINE_ERROR",
                "Local OCR engine returned non-zero exit code",
            )

        stdout = proc.stdout.strip()
        if not stdout:
            return []

        try:
            payload = json.loads(stdout)
        except Exception as exc:
            raise ImageOcrError("OCR_MALFORMED_OUTPUT", "OCR output could not be parsed") from exc

        raw_lines = payload.get("lines", [])
        result_lines: list[ImageOcrLine] = []
        for rl in raw_lines:
            text = str(rl.get("text", "")).strip()
            if not text:
                continue
            x = int(rl.get("x", 0))
            y = int(rl.get("y", 0)) + slice_y_offset
            w = int(rl.get("width", 0))
            h = int(rl.get("height", 0))
            result_lines.append(
                ImageOcrLine(
                    text=text,
                    x=x,
                    y=y,
                    width=w,
                    height=h,
                )
            )
        return result_lines


def _dedup_overlapping_lines(
    lines: Sequence[ImageOcrLine],
    *,
    spatial_threshold_y: int = 15,
) -> tuple[ImageOcrLine, ...]:
    """Deduplicates OCR lines resulting from slice overlap using spatial & text matching."""
    if not lines:
        return ()

    sorted_lines = sorted(lines, key=lambda l: (l.y, l.x))
    deduped: list[ImageOcrLine] = []

    for line in sorted_lines:
        is_duplicate = False
        for prev in reversed(deduped):
            # If previous line is far above, break early
            if line.y - prev.y > spatial_threshold_y:
                break
            # Check spatial proximity and normalized text match
            if (
                abs(line.y - prev.y) <= spatial_threshold_y
                and abs(line.x - prev.x) <= 30
                and " ".join(line.text.split()).casefold() == " ".join(prev.text.split()).casefold()
            ):
                is_duplicate = True
                break

        if not is_duplicate:
            deduped.append(line)

    return tuple(deduped)


def extract_image_text(
    image_bytes: bytes,
    *,
    config: ImageOcrConfig | None = None,
) -> ImageOcrResult:
    """Extracts structured OCR lines from an image payload using local Windows OCR."""
    if not image_bytes:
        raise ImageOcrError("EMPTY_PAYLOAD", "Cannot perform OCR on empty image payload")

    config = config or ImageOcrConfig()

    try:
        img = Image.open(BytesIO(image_bytes))
        img.load()
    except Exception as exc:
        raise ImageOcrError("IMAGE_DECODE_FAILED", "Failed to decode image payload") from exc

    width, height = img.size
    if width <= 0 or height <= 0:
        raise ImageOcrError("INVALID_DIMENSIONS", "Image dimensions must be positive")

    # If image height exceeds slice height, perform deterministic vertical slicing
    all_lines: list[ImageOcrLine] = []
    if height <= config.slice_height:
        all_lines = _extract_slice_windows_ocr(img, config=config, slice_y_offset=0)
    else:
        y_start = 0
        step = config.slice_height - config.slice_overlap
        while y_start < height:
            y_end = min(y_start + config.slice_height, height)
            slice_crop = img.crop((0, y_start, width, y_end))
            slice_lines = _extract_slice_windows_ocr(
                slice_crop,
                config=config,
                slice_y_offset=y_start,
            )
            all_lines.extend(slice_lines)
            if y_end >= height:
                break
            y_start += step

    deduped_lines = _dedup_overlapping_lines(all_lines)
    return ImageOcrResult(
        lines=deduped_lines,
        image_width=width,
        image_height=height,
    )
