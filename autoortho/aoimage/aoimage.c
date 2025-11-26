#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <assert.h>
#include <string.h>
#include <errno.h>
#include <turbojpeg.h>

#include "aoimage.h"

#define TRUE 1
#define FALSE 0

AOIAPI void aoimage_delete(aoimage_t *img) {
    // Prevent double-free by atomically clearing pointer first
    uint8_t *ptr_to_free = img->ptr;
    img->ptr = NULL;  // Clear pointer BEFORE freeing (atomic on x86/x64)
    
    if (ptr_to_free) {
        free(ptr_to_free);
    }
    memset(img, 0, sizeof(aoimage_t));
}

// create empty rgba image
AOIAPI int32_t aoimage_create(aoimage_t *img, uint32_t width, uint32_t height, uint32_t r, uint32_t g, uint32_t b) {
    memset(img, 0, sizeof(aoimage_t));

    // Runtime validation
    if (height < 4 || (height & 3) != 0) {
        sprintf(img->errmsg, "height %d must be >= 4 and multiple of 4", height);
        return FALSE;
    }
    if (width < 4 || (width & 3) != 0) {
        sprintf(img->errmsg, "width %d must be >= 4 and multiple of 4", width);
        return FALSE;
    }
    int len = width * height * 4;
    img->ptr = malloc(len);
    if (NULL == img->ptr) {
        sprintf(img->errmsg, "can't malloc %d bytes", len);
        return FALSE;
    }

    img->width = width;
    img->height = height;
    img->channels = 4;
    img->stride = width * 4;

    uint32_t pixel = 0xff000000 | (r & 0xff) | (g & 0xff) << 8 | (b & 0xff) << 16;

    if (pixel == 0xff000000) {
        // if pixel color is 0 alpha does not matter here so we zero out everything
        memset(img->ptr, 0, len);
    } else {
        // fill row 0 with integer arithmetics
        uint32_t *uiptr = (uint32_t *)img->ptr;
        while (uiptr < (uint32_t *)(img->ptr + img->stride))
            *uiptr++ = pixel;

        uint8_t *uptr = img->ptr + img->stride;
        memcpy(uptr, img->ptr, img->stride);        // copy row 1 from 0
        uptr += img->stride;

        memcpy(uptr, img->ptr, 2 * img->stride);    // copy 2 + 3 from 0 + 1
        uptr += 2 *img->stride;

        while (uptr < img->ptr + len) {             // fill rest from 0-4
            memcpy(uptr, img->ptr, 4 * img->stride);
            uptr += 4 * img->stride;
        }

        // Verify fill completed correctly (defensive check, not assert)
        if (uptr != img->ptr + len) {
            strcpy(img->errmsg, "internal error: fill length mismatch");
            free(img->ptr);
            img->ptr = NULL;
            return FALSE;
        }
    }

    return TRUE;
}

// dump header for debugging
AOIAPI void aoimage_dump(const char *title, const aoimage_t *img) {
    fprintf(stderr, "%s:\n\tptr:\t\%p\n\twidth:\t%d\n\theight\t%d\n\tstride\t%d\n\tchans:\t%d\n",
            title, img->ptr, img->width, img->height, img->stride, img->channels);
    //fflush(stderr);
}

// no longer really needed as jpeg-turbo already returns RGBA
AOIAPI int32_t aoimage_2_rgba(const aoimage_t *s_img, aoimage_t *d_img) {
    memset(d_img, 0, sizeof(aoimage_t));
    
    // Runtime validation
    if (s_img == NULL || s_img->ptr == NULL) {
        strcpy(d_img->errmsg, "source image is NULL");
        return FALSE;
    }
    
    // already 4 channels means copy
    if (4 == s_img->channels) {
        memcpy(d_img, s_img, sizeof(aoimage_t));
        int dlen = s_img->width * s_img->height * 4;
        d_img->ptr = malloc(dlen);
        if (NULL == d_img->ptr) {
            sprintf(d_img->errmsg, "can't malloc %d bytes", dlen);
            return FALSE;
        }
        memcpy(d_img->ptr, s_img->ptr, dlen);
        return TRUE;
    }

    if (s_img->channels != 3) {
        sprintf(d_img->errmsg, "channels must be 3 or 4, got %d", s_img->channels);
        return FALSE;
    }

    int slen = s_img->width * s_img->height * 3;
    int dlen = s_img->width * s_img->height * 4;
    uint8_t *dest = malloc(dlen);
    if (NULL == dest) {
		sprintf(d_img->errmsg, "can't malloc %d bytes", dlen);
        d_img->ptr = NULL;
        return FALSE;
    }

    const uint8_t *sptr = s_img->ptr;
    const uint8_t *send = sptr + slen;
    uint8_t *dptr = dest;
    while (sptr < send) {
        *dptr++ = *sptr++;
        *dptr++ = *sptr++;
        *dptr++ = *sptr++;
        *dptr++ = 0xff;
        //*dptr++ = 0x00;
    }

   d_img->ptr = dest;
   d_img->width = s_img->width;
   d_img->height = s_img->height;
   d_img->stride = 4 * d_img->width;
   d_img->channels = 4;
   return TRUE;
}

AOIAPI int32_t aoimage_read_jpg(const char *filename, aoimage_t *img) {
	long in_jpg_size;
	unsigned char *in_jpg_buff;

    FILE *fd = fopen(filename, "rb");
    if (fd == NULL) {
        strncpy(img->errmsg, strerror(errno), sizeof(img->errmsg)-1);
		return FALSE;
	}

    if (fseek(fd, 0, SEEK_END) < 0 || ((in_jpg_size = ftell(fd)) < 0) ||
            fseek(fd, 0, SEEK_SET) < 0) {
        strcpy(img->errmsg, "error determining input file size");
        return FALSE;
    }

    if (in_jpg_size == 0) {
        strcpy(img->errmsg, "inputfile has no data");
        return FALSE;
    }

    //fprintf(stderr, "File size %ld\n", in_jpg_size);
	in_jpg_buff = malloc(in_jpg_size);
    if (in_jpg_buff == NULL) {
		sprintf(img->errmsg, "can't malloc %ld bytes", in_jpg_size);
		return FALSE;
	}

    int rc = fread(in_jpg_buff, 1, in_jpg_size, fd);
    if (rc < 0) {
        strncpy(img->errmsg, strerror(errno), sizeof(img->errmsg)-1);
        return FALSE;
    }

    //fprintf(stderr, "Input: Read %d/%lu bytes\n", rc, in_jpg_size);
    fclose(fd);
    int res = aoimage_from_memory(img, in_jpg_buff, in_jpg_size);
    free(in_jpg_buff);
    return res;
}
AOIAPI int32_t aoimage_write_jpg(const char *filename, aoimage_t *img, int32_t quality) {
    tjhandle tjh = NULL;
    unsigned char *out_jpg_buf = NULL;
    unsigned long out_jpg_size = 0;
    FILE *fd = NULL;

    int result = FALSE;

    tjh = tjInitCompress();
    if (NULL == tjh) {
        strcpy(img->errmsg, "Can't allocate tjInitCompress");
        goto err;
    }

    int rc = tjCompress2(tjh, img->ptr, img->width, 0, img->height, TJPF_RGBA,
                         &out_jpg_buf, &out_jpg_size, TJSAMP_444, quality, 0);
    if (rc) {
        strncpy(img->errmsg, tjGetErrorStr2(tjh), sizeof(img->errmsg) - 1);
        goto err;
    }

    //fprintf(stderr, "jpg_size: %ld\n", out_jpg_size);
    fd = fopen(filename, "wb");
    if (fd == NULL) {
        strncpy(img->errmsg, strerror(errno), sizeof(img->errmsg)-1);
		goto err;
	}

    if (fwrite(out_jpg_buf, 1, out_jpg_size, fd) < 0) {
        strncpy(img->errmsg, strerror(errno), sizeof(img->errmsg)-1);
        goto err;
    }

    result = TRUE;

   err:
    if (fd) fclose(fd);
    if (tjh) tjDestroy(tjh);
    if (out_jpg_buf) tjFree(out_jpg_buf);
    return result;
}

AOIAPI int32_t aoimage_reduce_2(const aoimage_t *s_img, aoimage_t *d_img) {
    memset(d_img, 0, sizeof(aoimage_t));
    
    // Runtime validation
    if (s_img == NULL || s_img->ptr == NULL) {
        strcpy(d_img->errmsg, "source image is NULL");
        return FALSE;
    }
    
    if (s_img->channels != 4) {
        sprintf(d_img->errmsg, "channel error %d != 4", s_img->channels);
        return FALSE;
    }

    if (s_img->width < 4 || (s_img->width & 0x03) != 0) {
        sprintf(d_img->errmsg, "width error: %d", s_img->width);
        return FALSE;
    }

    //aoimage_dump("aoimage_reduce_2 s_img", s_img);

    int slen = s_img->width * s_img->height * 4;
    int dlen = slen / 4;
    uint8_t *dest = malloc(dlen);
    if (NULL == dest) {
		sprintf(d_img->errmsg, "can't malloc %d bytes", dlen);
        d_img->ptr = NULL;
        return FALSE;
    }

    const uint8_t *srptr = s_img->ptr;      // source row start
    const uint8_t *send = srptr + slen;
    uint8_t *dptr = dest;
    int stride = s_img->width * 4;

    // fprintf(stderr, "%p %d %d %d\n", sptr, slen, dlen, stride); fflush(stderr);
    while (srptr < send) {
        const uint8_t *sptr = srptr;
        while (sptr < srptr + stride) {
            uint8_t r = (sptr[0] + sptr[4] + sptr[stride] + sptr[stride + 4]) / 4;
            sptr++;
            uint8_t g = (sptr[0] + sptr[4] + sptr[stride] + sptr[stride + 4]) / 4;
            sptr++;
            uint8_t b = (sptr[0] + sptr[4] + sptr[stride] + sptr[stride + 4]) / 4;
            sptr += 1 + 1 + 4;  // skip over alpha + next RGBA

            *dptr++ = r;
            *dptr++ = g;
            *dptr++ = b;
            *dptr++ = 0xff;
            //*dptr++ = 0x00;
            // Bounds check (defensive, replaces assert)
            if (dptr > dest + dlen) {
                strcpy(d_img->errmsg, "reduce_2 buffer overflow");
                free(dest);
                d_img->ptr = NULL;
                return FALSE;
            }
        }
        srptr += 2* stride;
    }
    d_img->ptr = dest;
    d_img->width = s_img->width / 2;
    d_img->height = s_img->height / 2;
    d_img->stride = 4 * d_img->width;
    d_img->channels = 4;

    // Final verification (defensive, replaces assert)
    if (dptr != dest + dlen || dlen != d_img->width * d_img->height * 4) {
        strcpy(d_img->errmsg, "reduce_2 size mismatch");
        free(dest);
        d_img->ptr = NULL;
        return FALSE;
    }
    return TRUE;
}

AOIAPI int32_t aoimage_scale(const aoimage_t *s_img, aoimage_t *d_img, uint32_t factor) {
    memset(d_img, 0, sizeof(aoimage_t));
    
    // Runtime validation (replaces asserts that are disabled in release builds)
    if (s_img == NULL || s_img->ptr == NULL) {
        strcpy(d_img->errmsg, "source image is NULL");
        return FALSE;
    }
    
    if (s_img->channels != 4) {
        sprintf(d_img->errmsg, "invalid channels: %d != 4", s_img->channels);
        return FALSE;
    }

    if (s_img->width < 4 || (s_img->width & 0x03) != 0) {
        sprintf(d_img->errmsg, "invalid width: %d", s_img->width);
        return FALSE;
    }

    if (factor == 0) {
        strcpy(d_img->errmsg, "invalid scale factor");
        return FALSE;
    }

    uint32_t src_w = s_img->width;
    uint32_t src_h = s_img->height;
    uint32_t dst_w = src_w * factor;
    uint32_t dst_h = src_h * factor;

    unsigned long long num_pixels = (unsigned long long)dst_w * (unsigned long long)dst_h;
    unsigned long long num_bytes = num_pixels * 4ULL;
    if (num_pixels == 0ULL || (num_bytes / 4ULL) != num_pixels) {
        strcpy(d_img->errmsg, "scale overflow");
        d_img->ptr = NULL;
        return FALSE;
    }

    uint32_t *dest = (uint32_t *)malloc((size_t)num_bytes);
    if (NULL == dest) {
        sprintf(d_img->errmsg, "can't malloc %llu bytes", num_bytes);
        d_img->ptr = NULL;
        return FALSE;
    }

    const uint32_t *src = (const uint32_t *)s_img->ptr;
    for (uint32_t sy = 0; sy < src_h; ++sy) {
        for (uint32_t sx = 0; sx < src_w; ++sx) {
            uint32_t px = src[sy * src_w + sx];
            uint32_t dy0 = sy * factor;
            uint32_t dx0 = sx * factor;
            uint32_t base = dy0 * dst_w + dx0;
            for (uint32_t fy = 0; fy < factor; ++fy) {
                uint32_t row_base = base + fy * dst_w;
                for (uint32_t fx = 0; fx < factor; ++fx) {
                    dest[row_base + fx] = px;
                }
            }
        }
    }

    d_img->ptr = (uint8_t *)dest;
    d_img->width = dst_w;
    d_img->height = dst_h;
    d_img->stride = 4 * d_img->width;
    d_img->channels = 4;
    return TRUE;
}

AOIAPI int32_t aoimage_copy(const aoimage_t *s_img, aoimage_t *d_img, uint32_t s_height_only) {
    memset(d_img, 0, sizeof(aoimage_t));
    
    // Runtime validation (replaces asserts)
    if (s_img == NULL || s_img->ptr == NULL) {
        strcpy(d_img->errmsg, "source image is NULL");
        return FALSE;
    }
    
    if (s_height_only > s_img->height) {
        sprintf(d_img->errmsg, "height_only %d > height %d", s_height_only, s_img->height);
        return FALSE;
    }

    if (0 == s_height_only)
        s_height_only = s_img->height;

    int dlen = s_img->width * s_height_only * s_img->channels;
    uint8_t *dest = malloc(dlen);
    if (NULL == dest) {
		sprintf(d_img->errmsg, "can't malloc %d bytes", dlen);
        d_img->ptr = NULL;
        return FALSE;
    }

    memcpy(dest, s_img->ptr, dlen);
    d_img->ptr = dest;
    d_img->width = s_img->width;
    d_img->height = s_height_only;
    d_img->stride = 4 * d_img->width;
    d_img->channels = 4;
    d_img->errmsg[0] = '\0';
    return TRUE;

}

AOIAPI int32_t aoimage_from_memory(aoimage_t *img, const uint8_t *data, uint32_t len) {
    memset(img, 0, sizeof(aoimage_t));

    // Validate input parameters to prevent access violations
    if (data == NULL) {
        strcpy(img->errmsg, "data pointer is NULL");
        return FALSE;
    }
    
    if (len < 4) {
        strcpy(img->errmsg, "data too short (< 4 bytes)");
        return FALSE;
    }

    // Check JPEG signature (FFD8FF)
    uint32_t signature = *(uint32_t *)data & 0x00ffffff;

    if (signature != 0x00ffd8ff) {
        strcpy(img->errmsg, "data is not a JPEG");
        return FALSE;
    }

    tjhandle tjh = NULL;
    unsigned char *img_buff = NULL;

    tjh = tjInitDecompress();
    if (NULL == tjh) {
        strcpy(img->errmsg, "Can't allocate tjInitDecompress");
        goto err;
    }

    int subsamp, width, height, color_space;

    if (tjDecompressHeader3(tjh, data, len, &width, &height, &subsamp, &color_space) < 0) {
        const char *err_str = tjGetErrorStr2(tjh);
        if (err_str != NULL) {
            strncpy(img->errmsg, err_str, sizeof(img->errmsg) - 1);
            img->errmsg[sizeof(img->errmsg) - 1] = '\0';  // Ensure null termination
        } else {
            strcpy(img->errmsg, "tjDecompressHeader3 failed (no error string)");
        }
        goto err;
    }
    
    // Validate dimensions to prevent allocation issues
    if (width <= 0 || height <= 0 || width > 65536 || height > 65536) {
        sprintf(img->errmsg, "invalid dimensions: %dx%d", width, height);
        goto err;
    }

    //fprintf(stderr, "%d %d %d\n", width, height, subsamp); fflush(stderr);

    unsigned long img_size = width * height * tjPixelSize[TJPF_RGBA];
    //fprintf(stderr, "img_size %ld bytes\n", img_size);
    img_buff = malloc(img_size);
    if (img_buff == NULL) {
		sprintf(img->errmsg, "can't malloc %ld bytes", img_size);
		goto err;
	}

    //printf("Pixel format: %d\n", TJPF_RGBA);

    if (tjDecompress2(tjh, data, len, img_buff, width, 0, height, TJPF_RGBA, TJFLAG_FASTDCT) < 0) {
        const char *err_str = tjGetErrorStr2(tjh);
        if (err_str != NULL) {
            strncpy(img->errmsg, err_str, sizeof(img->errmsg) - 1);
            img->errmsg[sizeof(img->errmsg) - 1] = '\0';  // Ensure null termination
        } else {
            strcpy(img->errmsg, "tjDecompress2 failed (no error string)");
        }
        goto err;
    }

    tjDestroy(tjh);

    img->ptr = img_buff;
    img->width = width;
    img->height = height;
    img->channels = 4;
    img->stride = img->channels * img->width;
    return TRUE;

err:
    if (tjh) tjDestroy(tjh);
    if (img_buff) free(img_buff);
    return FALSE;
}

AOIAPI void aoimage_tobytes(aoimage_t *img, uint8_t *data) {
    memcpy(data, img->ptr, img->width * img->height * img->channels);
}

AOIAPI int32_t aoimage_paste(aoimage_t *img, const aoimage_t *p_img, uint32_t x, uint32_t y) {
    // Runtime validation (replaces asserts)
    if (img == NULL || img->ptr == NULL) {
        if (img) strcpy(img->errmsg, "destination image is NULL");
        return FALSE;
    }
    if (p_img == NULL || p_img->ptr == NULL) {
        strcpy(img->errmsg, "source image is NULL");
        return FALSE;
    }
    if (x + p_img->width > img->width || y + p_img->height > img->height) {
        sprintf(img->errmsg, "paste out of bounds: (%d,%d)+(%dx%d) > %dx%d",
                x, y, p_img->width, p_img->height, img->width, img->height);
        return FALSE;
    }
    if (img->channels != 4 || p_img->channels != 4) {
        strcpy(img->errmsg, "both images must have 4 channels");
        return FALSE;
    }

    //aoimage_dump("paste img", img);
    //aoimage_dump("paste P", p_img);
    //fprintf(stderr, "aoimage_paste: %d %d\n", x, y);

    uint8_t *ip = img->ptr + (y * img->width * 4) + x * 4;  // lower left corner of image
    uint8_t *pp = p_img->ptr;

    for (int i = 0; i < p_img->height; i++) {
        memcpy(ip, pp, p_img->width * 4);
        ip += img->width * 4;
        pp += p_img->width * 4;
    }

    return TRUE;
}

AOIAPI int32_t aoimage_crop(aoimage_t *img, const aoimage_t *c_img, uint32_t x, uint32_t y) {
    // Runtime validation (replaces asserts)
    if (img == NULL || img->ptr == NULL) {
        // Can't write error to c_img (const) - just return failure
        return FALSE;
    }
    if (c_img == NULL || c_img->ptr == NULL) {
        strcpy(img->errmsg, "destination image is NULL");
        return FALSE;
    }
    if (x + c_img->width > img->width || y + c_img->height > img->height) {
        sprintf(img->errmsg, "crop out of bounds: (%d,%d)+(%dx%d) > %dx%d",
                x, y, c_img->width, c_img->height, img->width, img->height);
        return FALSE;
    }
    if (img->channels != 4 || c_img->channels != 4) {
        strcpy(img->errmsg, "both images must have 4 channels");
        return FALSE;
    }

    uint8_t *ip = img->ptr + (y * img->width * 4) + x * 4;  // lower left corner of image
    uint8_t *cp = c_img->ptr;

    for (int i = 0; i < c_img->height; i++) {
        memcpy(cp, ip, c_img->width * 4);
        ip += img->width * 4;
        cp += c_img->width * 4;
    }

    return TRUE;
}

AOIAPI int32_t aoimage_desaturate(aoimage_t *img, float saturation) {
    // Runtime validation
    if (img == NULL || img->ptr == NULL) {
        if (img) strcpy(img->errmsg, "image is NULL");
        return FALSE;
    }
    if (img->channels != 4) {
        sprintf(img->errmsg, "channels must be 4, got %d", img->channels);
        return FALSE;
    }
    if (saturation < 0.0f || saturation > 1.0f) {
        strcpy(img->errmsg, "saturation must be 0.0-1.0");
        return FALSE;
    }

    int len = img->width * img->height * 4;
    for (uint8_t *ptr = img->ptr; ptr < img->ptr + len; ptr += 4) {
        float luma = 0.212671f * ptr[0] + 0.715160f * ptr[1] + 0.072169f * ptr[2];
        float x = (1.0f - saturation) * luma;
        ptr[0] = (uint8_t)(saturation * ptr[0] + x);
        ptr[1] = (uint8_t)(saturation * ptr[1] + x);
        ptr[2] = (uint8_t)(saturation * ptr[2] + x);
    }

    return TRUE;
}

AOIAPI int32_t aoimage_crop_and_upscale(aoimage_t *src_img, aoimage_t *dst_img, 
                                        uint32_t crop_x, uint32_t crop_y,
                                        uint32_t crop_width, uint32_t crop_height,
                                        uint32_t scale_factor) {
    memset(dst_img, 0, sizeof(aoimage_t));
    
    // Validate inputs (runtime checks, not asserts)
    if (src_img == NULL || src_img->ptr == NULL) {
        strcpy(dst_img->errmsg, "source image is NULL");
        return FALSE;
    }
    
    if (src_img->channels != 4) {
        sprintf(dst_img->errmsg, "channels must be 4, got %d", src_img->channels);
        return FALSE;
    }
    
    if (scale_factor == 0 || (scale_factor & (scale_factor - 1)) != 0) {
        strcpy(dst_img->errmsg, "scale_factor must be power of 2");
        return FALSE;
    }
    
    // Bounds check
    if (crop_x + crop_width > src_img->width) {
        sprintf(dst_img->errmsg, "crop x bounds: %u + %u > %u", crop_x, crop_width, src_img->width);
        return FALSE;
    }
    
    if (crop_y + crop_height > src_img->height) {
        sprintf(dst_img->errmsg, "crop y bounds: %u + %u > %u", crop_y, crop_height, src_img->height);
        return FALSE;
    }
    
    // Calculate destination dimensions
    uint32_t dst_width = crop_width * scale_factor;
    uint32_t dst_height = crop_height * scale_factor;
    
    // Check for overflow
    unsigned long long num_pixels = (unsigned long long)dst_width * (unsigned long long)dst_height;
    unsigned long long num_bytes = num_pixels * 4ULL;
    if (num_pixels == 0ULL || (num_bytes / 4ULL) != num_pixels) {
        strcpy(dst_img->errmsg, "destination size overflow");
        return FALSE;
    }
    
    // Allocate destination buffer
    uint8_t *dest = malloc((size_t)num_bytes);
    if (NULL == dest) {
        sprintf(dst_img->errmsg, "can't malloc %llu bytes", num_bytes);
        return FALSE;
    }
    
    // Perform crop and upscale in one pass (nearest-neighbor)
    // Read from source crop region, write each pixel scale_factor times in each direction
    uint8_t *dst_ptr = dest;
    
    for (uint32_t src_y = 0; src_y < crop_height; ++src_y) {
        uint32_t src_row_offset = ((crop_y + src_y) * src_img->width + crop_x) * 4;
        
        for (uint32_t rep_y = 0; rep_y < scale_factor; ++rep_y) {
            for (uint32_t src_x = 0; src_x < crop_width; ++src_x) {
                // Get source pixel
                uint32_t src_offset = src_row_offset + src_x * 4;
                uint8_t r = src_img->ptr[src_offset];
                uint8_t g = src_img->ptr[src_offset + 1];
                uint8_t b = src_img->ptr[src_offset + 2];
                uint8_t a = src_img->ptr[src_offset + 3];
                
                // Replicate pixel scale_factor times horizontally
                for (uint32_t rep_x = 0; rep_x < scale_factor; ++rep_x) {
                    *dst_ptr++ = r;
                    *dst_ptr++ = g;
                    *dst_ptr++ = b;
                    *dst_ptr++ = a;
                }
            }
        }
    }
    
    // Set destination image properties
    dst_img->ptr = dest;
    dst_img->width = dst_width;
    dst_img->height = dst_height;
    dst_img->stride = dst_width * 4;
    dst_img->channels = 4;
    
    // Verify write completed correctly (defensive check, replaces assert)
    if (dst_ptr != dest + num_bytes) {
        strcpy(dst_img->errmsg, "crop_and_upscale size mismatch");
        free(dest);
        dst_img->ptr = NULL;
        return FALSE;
    }
    return TRUE;
}