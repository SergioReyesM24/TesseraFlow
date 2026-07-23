import { afterEach, describe, expect, it, vi } from 'vitest'
import { PcmPlayer, pcm16ToFloat32 } from './audio'

class FakeAudioBuffer {
  /** Allocate one writable mono channel for the player. */
  constructor(readonly samples: Float32Array, readonly duration: number) {}

  /** Return the mono destination expected by the PCM player. */
  getChannelData(): Float32Array {
    return this.samples
  }
}

class FakeAudioSource {
  buffer: AudioBuffer | null = null
  onended: (() => void) | null = null
  startedAt: number | null = null

  /** Accept the fake audio destination. */
  connect(): void {}

  /** Capture the scheduled playback time. */
  start(when?: number): void {
    this.startedAt = when ?? 0
  }

  /** Model an interruptible source without additional behavior. */
  stop(): void {}
}

class FakeAudioContext {
  static instances: FakeAudioContext[] = []
  static initialState: AudioContextState = 'running'

  state = FakeAudioContext.initialState
  currentTime = 1
  destination = {}
  resumeCount = 0
  sources: FakeAudioSource[] = []

  /** Retain every fake context so lifecycle behavior can be asserted. */
  constructor() {
    FakeAudioContext.instances.push(this)
  }

  /** Create a mono buffer with the requested duration. */
  createBuffer(_channels: number, length: number, sampleRate: number): AudioBuffer {
    return new FakeAudioBuffer(
      new Float32Array(length),
      length / sampleRate,
    ) as unknown as AudioBuffer
  }

  /** Capture sources created for playback. */
  createBufferSource(): AudioBufferSourceNode {
    const source = new FakeAudioSource()
    this.sources.push(source)
    return source as unknown as AudioBufferSourceNode
  }

  /** Resume a browser-suspended context. */
  async resume(): Promise<void> {
    this.resumeCount += 1
    this.state = 'running'
  }

  /** Close the fake context. */
  async close(): Promise<void> {
    this.state = 'closed'
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
  FakeAudioContext.instances = []
  FakeAudioContext.initialState = 'running'
})

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

describe('PCM playback lifecycle', () => {
  it('resumes a suspended context before accepting audio', async () => {
    FakeAudioContext.initialState = 'suspended'
    vi.stubGlobal('AudioContext', FakeAudioContext)
    const player = new PcmPlayer()

    await player.resume()

    expect(FakeAudioContext.instances[0]?.resumeCount).toBe(1)
  })

  it('recreates a closed context and schedules received PCM', async () => {
    vi.stubGlobal('AudioContext', FakeAudioContext)
    const player = new PcmPlayer()
    await player.resume()
    FakeAudioContext.instances[0]!.state = 'closed'
    const pcm = new ArrayBuffer(4)
    new DataView(pcm).setInt16(0, 8192, true)

    await player.enqueue(pcm, 24_000)

    expect(FakeAudioContext.instances).toHaveLength(2)
    expect(FakeAudioContext.instances[1]?.sources[0]?.startedAt).toBeCloseTo(1.015)
  })
})
