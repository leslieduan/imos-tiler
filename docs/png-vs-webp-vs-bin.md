# Tile format evaluation: PNG vs WebP vs Binary

## Context

Tiles are RGBA images where pixel channels encode ocean data values rather than visual colours. They are consumed directly as WebGL2 textures. The question is which format best balances file size, encode time, and client-side simplicity.

## Formats considered

### PNG (current)

PNG stores raw RGBA uint8 bytes compressed with deflate. The `optimize=False` flag ensures pixel values are written exactly as-is, which is required for shader decoding.

- Encode time: ~8ms
- File size: ~115KB (benchmarked on a 240×192 SSTA tile)
- Client: `createImageBitmap(blob)` → `texImage2D`

### WebP lossless

WebP lossless uses a more sophisticated compression algorithm than PNG's deflate. It produces smaller files but at significantly higher CPU cost.

Benchmarked on the same 240×192 SSTA tile:

|             | PNG   | WebP lossless |
| ----------- | ----- | ------------- |
| Encode time | 8.7ms | 1005ms        |
| File size   | 115KB | 93KB          |
| Size saving | —     | 19%           |

**Verdict: not viable.** A 115× increase in encode time for a 19% file size saving is a poor tradeoff. The 1-second encode latency would dominate the entire tile pipeline.

Note: WebP lossy is not considered at all — even minor lossy compression corrupts the encoded data values (uint24 scalars packed across RGB channels), making shader decoding produce wrong results.

### Raw binary (RGBA uint8)

Instead of an image format, serve the raw pixel bytes as a flat binary buffer. The client loads it as an `ArrayBuffer` and passes it directly to `texImage2D`.

```
PNG  = deflate_compressed(RGBA uint8) + headers   ~115KB
Bin  = RGBA uint8 raw                             ~180KB
Bin  + HTTP gzip                                  ~125–130KB
```

- Raw binary is ~56% larger than PNG (180KB vs 115KB)
- With HTTP gzip enabled on the server, the gap closes to ~10% larger — gzip uses the same deflate algorithm as PNG, but PNG also applies per-row filters before compression, which improves the compression ratio on image-like data, so PNG still wins slightly

The only gain over PNG is skipping `createImageBitmap` on the client:

```js
// PNG
fetch(url) → blob() → createImageBitmap() → texImage2D()

// Binary
fetch(url) → arrayBuffer() → new Uint8Array(buf) → texImage2D()
```

`createImageBitmap` is async and runs off the main thread in modern browsers — its cost is typically 2–10ms per tile and is rarely a bottleneck.

**Verdict: not worth it.** Binary is larger than PNG and the client-side saving is negligible.

### Raw binary (float32)

Store actual float32 values per pixel instead of uint8. This removes the normalisation encoding entirely and simplifies the shader — no uint24 unpacking, no `valueRange` needed.

- File size: ~360KB raw, ~150–200KB gzipped
- Encode time: fast (no compression)
- Shader: simpler — read float directly, no unpacking

**Verdict: not worth it for visualisation.** uint8 precision (256 levels) is sufficient for colour-mapped ocean data display. The 3× file size increase is not justified by the shader simplification.

## Conclusion

PNG is the right format for this use case:

- Fast to encode (~8ms)
- Good compression via internal deflate (~115KB for a 240×192 tile)
- Exact pixel values preserved (`optimize=False`)
- Standard browser support — no special client handling needed
