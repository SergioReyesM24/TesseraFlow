/** Convert little-endian PCM16 bytes into normalized Web Audio samples. */
export function pcm16ToFloat32(bytes: ArrayBuffer): Float32Array {
  const source = new DataView(bytes)
  const output = new Float32Array(Math.floor(bytes.byteLength / 2))
  for (let index = 0; index < output.length; index += 1) {
    const sample = source.getInt16(index * 2, true)
    output[index] = sample < 0 ? sample / 32768 : sample / 32767
  }
  return output
}

/** Decode a base64 PCM payload emitted by the durable text WebSocket. */
export function base64ToArrayBuffer(value: string): ArrayBuffer {
  const decoded = window.atob(value)
  const bytes = new Uint8Array(decoded.length)
  for (let index = 0; index < decoded.length; index += 1) {
    bytes[index] = decoded.charCodeAt(index)
  }
  return bytes.buffer
}

/** Schedule raw PCM chunks without gaps and support immediate barge-in clearing. */
export class PcmPlayer {
  private context: AudioContext | null = null
  private nextStartTime = 0
  private sources = new Set<AudioBufferSourceNode>()

  /** Unlock audio playback from a user gesture when the browser requires it. */
  async resume(): Promise<void> {
    const context = this.getContext()
    if (context.state === 'suspended') {
      await context.resume()
    }
    if (context.state !== 'running') {
      throw new Error(`El contexto de audio está ${context.state}`)
    }
  }

  /** Queue one mono PCM16 chunk at its declared sample rate. */
  async enqueue(bytes: ArrayBuffer, sampleRate = 24_000): Promise<void> {
    if (bytes.byteLength < 2) return
    await this.resume()
    const context = this.getContext()
    const samples = pcm16ToFloat32(bytes)
    const buffer = context.createBuffer(1, samples.length, sampleRate)
    buffer.getChannelData(0).set(samples)
    const source = context.createBufferSource()
    source.buffer = buffer
    source.connect(context.destination)
    this.nextStartTime = Math.max(this.nextStartTime, context.currentTime + 0.015)
    source.start(this.nextStartTime)
    this.nextStartTime += buffer.duration
    this.sources.add(source)
    source.onended = () => this.sources.delete(source)
  }

  /** Stop all pending assistant audio after an interruption event. */
  clear(): void {
    for (const source of this.sources) {
      try {
        source.stop()
      } catch {
        // An already-ended source is safe to ignore during queue clearing.
      }
    }
    this.sources.clear()
    this.nextStartTime = this.context?.currentTime ?? 0
  }

  /** Release browser audio resources when the view is disposed. */
  async close(): Promise<void> {
    this.clear()
    if (this.context && this.context.state !== 'closed') {
      await this.context.close()
    }
    this.context = null
  }

  /** Lazily create the playback context to avoid autoplay-policy failures on load. */
  private getContext(): AudioContext {
    if (!this.context || this.context.state === 'closed') {
      this.context = new AudioContext({ latencyHint: 'interactive' })
      this.nextStartTime = 0
      this.sources.clear()
    }
    return this.context
  }
}

/** Capture microphone audio and deliver resampled PCM16 chunks at 16 kHz. */
export class MicrophoneCapture {
  private context: AudioContext | null = null
  private stream: MediaStream | null = null
  private source: MediaStreamAudioSourceNode | null = null
  private worklet: AudioWorkletNode | null = null
  private silentGain: GainNode | null = null

  /** Request microphone access and begin producing transferable PCM chunks. */
  async start(onChunk: (chunk: ArrayBuffer) => void): Promise<void> {
    if (this.stream) return
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    })
    const context = new AudioContext({ latencyHint: 'interactive' })

    try {
      await context.audioWorklet.addModule('/pcm-capture.worklet.js')
      const source = context.createMediaStreamSource(stream)
      const worklet = new AudioWorkletNode(context, 'pcm-capture-processor', {
        processorOptions: { targetSampleRate: 16_000 },
      })
      const silentGain = context.createGain()
      silentGain.gain.value = 0
      worklet.port.onmessage = (event: MessageEvent<ArrayBuffer>) => onChunk(event.data)
      source.connect(worklet)
      worklet.connect(silentGain)
      silentGain.connect(context.destination)
      if (context.state === 'suspended') await context.resume()
      this.context = context
      this.stream = stream
      this.source = source
      this.worklet = worklet
      this.silentGain = silentGain
    } catch (error) {
      stream.getTracks().forEach((track) => track.stop())
      await context.close()
      throw error
    }
  }

  /** Stop capture and release the microphone immediately. */
  async stop(): Promise<void> {
    this.stream?.getTracks().forEach((track) => track.stop())
    this.source?.disconnect()
    this.worklet?.disconnect()
    this.silentGain?.disconnect()
    this.worklet = null
    this.source = null
    this.silentGain = null
    this.stream = null
    if (this.context && this.context.state !== 'closed') await this.context.close()
    this.context = null
  }
}
