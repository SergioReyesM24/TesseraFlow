class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super()
    this.targetSampleRate = options.processorOptions?.targetSampleRate ?? 16000
    this.ratio = sampleRate / this.targetSampleRate
    this.buffer = new Float32Array(0)
    this.position = 0
    this.chunkSamples = Math.round(this.targetSampleRate * 0.02)
    this.pendingSamples = []
  }

  /** Resample browser-rate mono audio and emit transferable PCM16 chunks. */
  process(inputs) {
    const channel = inputs[0]?.[0]
    if (!channel?.length) return true

    const combined = new Float32Array(this.buffer.length + channel.length)
    combined.set(this.buffer)
    combined.set(channel, this.buffer.length)
    const samples = []

    while (this.position + 1 < combined.length) {
      const left = Math.floor(this.position)
      const fraction = this.position - left
      const value = combined[left] + (combined[left + 1] - combined[left]) * fraction
      samples.push(Math.max(-1, Math.min(1, value)))
      this.position += this.ratio
    }

    // Retain one boundary sample so interpolation remains continuous across blocks.
    const retainedFrom = combined.length - 1
    this.buffer = combined.slice(retainedFrom)
    this.position -= retainedFrom

    this.pendingSamples.push(...samples)
    while (this.pendingSamples.length >= this.chunkSamples) {
      const chunk = this.pendingSamples.splice(0, this.chunkSamples)
      const pcm = new Int16Array(chunk.length)
      for (let index = 0; index < chunk.length; index += 1) {
        const value = chunk[index]
        pcm[index] = value < 0 ? value * 32768 : value * 32767
      }
      this.port.postMessage(pcm.buffer, [pcm.buffer])
    }
    return true
  }
}

registerProcessor('pcm-capture-processor', PcmCaptureProcessor)
