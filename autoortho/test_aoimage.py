#!/usr/bin/env python3

import os
import ctypes
import platform

import pytest
#from PIL import Image
from aoimage import AoImage

TESTPNG=os.path.join('testfiles', 'test_tile.png')
TESTJPG=os.path.join('testfiles', 'test_tile2.jpg')
TESTSMALLJPG=os.path.join('testfiles', 'test_tile_small.jpg')

def test_aoimage_create(tmpdir):
    img = AoImage.new('RGBA', (4096,4096), (0,0,0))

    data = ctypes.cast(img._data, ctypes.POINTER(ctypes.c_uint8))
    print(type(img._data))
    print(type(data))
    assert data[:16] == [0]*16

    img = AoImage.new('RGBA', (4096,4096), (255,0,0))
    data = ctypes.cast(img._data, ctypes.POINTER(ctypes.c_uint8))
    assert data[:16] == [255,0,0,255]*4
    
    img = AoImage.new('RGBA', (4096,4096), (0,255,0))
    data = ctypes.cast(img._data, ctypes.POINTER(ctypes.c_uint8))
    assert data[:16] == [0,255,0,255]*4


def test_aoimage_open(tmpdir):
    img = AoImage.open(TESTJPG)
    data = ctypes.cast(img._data, ctypes.POINTER(ctypes.c_uint8))
    assert data[:8] != [0]*8


def test_aoimage_reduce(tmpdir):
    img = AoImage.open(TESTJPG)
    small = img.reduce_2(1)

    assert small._width == 2048
    assert small._height == 2048

    small.write_jpg(os.path.join(tmpdir, 'test.jpg'))

@pytest.mark.parametrize("width,height,steps", [
    (1024, 512, 1),  # Clamped zoom, one partial DDS row.
    (1024, 768, 1),  # Several partial rows.
    (512, 1024, 2),
    (1024, 256, 3),
    (1024, 1024, 3),
    (6, 4, 1),  # Even dimensions need not be multiples of four.
    (4, 4, 2),
])
def test_aoimage_reduce_partial_rows(width, height, steps):
    """Exercise the native reducer used by partial mipmap composition."""
    with AoImage.new('RGBA', (width, height), (20, 40, 60)) as img:
        with img.reduce_2(steps) as reduced:
            assert reduced.size == (width >> steps, height >> steps)
            assert reduced._stride == reduced._width * 4
            pixels = ctypes.string_at(
                reduced._data, reduced._stride * reduced._height
            )
            assert pixels == bytes([20, 40, 60, 255]) * (
                reduced._width * reduced._height
            )


def test_aoimage_reduce_rectangular_pixel_averages():
    with AoImage.new('RGBA', (8, 4), (20, 40, 60)) as img:
        pixels = (ctypes.c_uint8 * (8 * 4 * 4)).from_address(img._data)
        for y in range(4):
            for x in range(8):
                offset = (y * 8 + x) * 4
                pixels[offset:offset + 4] = [x * 10, y * 20, 240, 255]
        with img.reduce_2() as reduced:
            expected = bytes(
                value
                for y in range(2)
                for x in range(4)
                for value in (x * 20 + 5, y * 40 + 10, 240, 255)
            )
            assert ctypes.string_at(reduced._data, len(expected)) == expected


@pytest.mark.parametrize("width,height", [
    (0, 4), (1, 4), (3, 4), (4, 0), (4, 1), (4, 3),
])
def test_aoimage_reduce_rejects_incomplete_pixel_blocks(width, height):
    # No allocation: invalid dimensions must be rejected before reading pixels.
    with AoImage.AoImage() as img:
        img._width, img._height, img._channels = width, height, 4
        with pytest.raises(AoImage.AOImageException):
            img.reduce_2()


def test_aoimage_double(tmpdir):
    img = AoImage.open(TESTSMALLJPG)
    big = img.scale(2)

    assert big._width == 512
    assert big._height == 512

    big.write_jpg(os.path.join(tmpdir, 'test.jpg'))

def test_aoimage_scale4(tmpdir):
    img = AoImage.open(TESTSMALLJPG)
    big = img.scale(4)

    assert big._width == 1024
    assert big._height == 1024

    big.write_jpg(os.path.join(tmpdir, 'test.jpg'))
