import { describe, expect, it } from 'vitest'
import { pcm16ToFloat32 } from './audio'

describe('PCM16 decoding', () => {
  it('maps signed little-endian samples to normalized Web Audio values', () => {
    const input = new ArrayBuffer(8)
    const view = new DataView(input)
    view.setInt16(0, -32768, true)
    view.setInt16(2, 0, true)
    view.setInt16(4, 16384, true)
    view.setInt16(6, 32767, true)

    const output = pcm16ToFloat32(input)
    expect(output[0]).toBe(-1)
    expect(output[1]).toBe(0)
    expect(output[2]).toBeCloseTo(16384 / 32767, 6)
    expect(output[3]).toBe(1)
  })

  it('ignores a trailing incomplete byte', () => {
    expect(pcm16ToFloat32(new ArrayBuffer(3))).toHaveLength(1)
  })
})
