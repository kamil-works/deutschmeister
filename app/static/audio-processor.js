/**
 * AudioWorklet Processor: Float32 → Int16 PCM + 16kHz'e resample.
 *
 * CLAUDE.md Audio Format gereksinimleri:
 *   - Giriş: 16 kHz mono PCM Int16 little-endian
 *   - AudioContext native rate'i 44100 veya 48000 olabilir → burada downsample
 *   - Float32 [-1.0, 1.0] → Int16 [-32768, 32767]
 *   - Her chunk ~20ms → 320 sample @ 16kHz
 *
 * NEDEN AudioContext({ sampleRate: 16000 }) YETMEZ:
 *   Firefox hata verir, Bluetooth kulaklıkta anında değişir.
 *   Bu yüzden native rate'de açıp burada manuel resample yapıyoruz.
 */
class AudioProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    // Hedef: Gemini'nin beklediği 16.000 Hz
    this._targetRate = options?.processorOptions?.targetSampleRate ?? 16000;
    // Kaynak: AudioContext'in gerçek rate'i (sampleRate = AudioWorkletProcessor global)
    this._sourceRate = sampleRate;
    // Downsample oranı (örn: 48000/16000 = 3.0)
    this._ratio = this._sourceRate / this._targetRate;
    // Gelen sample'ları biriktiren buffer (resample için)
    this._buffer = new Float32Array(8192);
    this._bufferLen = 0;

    // Debug: rate bilgisini main thread'e bildir
    this.port.postMessage({
      type: 'init',
      sourceRate: this._sourceRate,
      targetRate: this._targetRate,
      ratio: this._ratio,
    });
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0] || !input[0].length) return true;

    const channelData = input[0]; // Float32Array

    // Gelen sample'ları buffer'a ekle
    for (let i = 0; i < channelData.length; i++) {
      if (this._bufferLen < this._buffer.length) {
        this._buffer[this._bufferLen++] = channelData[i];
      }
    }

    // Her ~20ms'lik 16kHz chunk için gereken kaynak sample sayısı
    const targetChunkSize = 320; // 16000 * 0.020
    const sourceChunkSize = Math.floor(this._ratio * targetChunkSize);

    // Buffer'da yeterli veri biriktikçe chunk'ları gönder
    while (this._bufferLen >= sourceChunkSize) {
      const chunk = this._buffer.slice(0, sourceChunkSize);

      // Basit linear interpolasyon ile downsample
      const resampled = new Float32Array(targetChunkSize);
      for (let i = 0; i < targetChunkSize; i++) {
        const srcPos = i * this._ratio;
        const srcIdx = Math.floor(srcPos);
        const frac = srcPos - srcIdx;
        const a = chunk[srcIdx] ?? 0;
        const b = chunk[Math.min(srcIdx + 1, sourceChunkSize - 1)] ?? 0;
        // Linear interpolasyon — basit ama yeterli kalite
        resampled[i] = a + frac * (b - a);
      }

      // Float32 → Int16 dönüşümü
      // CLAUDE.md: clamped < 0 → * 0x8000, >= 0 → * 0x7FFF
      const int16 = new Int16Array(targetChunkSize);
      for (let i = 0; i < targetChunkSize; i++) {
        const clamped = Math.max(-1.0, Math.min(1.0, resampled[i]));
        int16[i] = clamped < 0 ? (clamped * 0x8000) | 0 : (clamped * 0x7FFF) | 0;
      }

      // Transferable ArrayBuffer ile sıfır-kopya gönderim
      const buf = int16.buffer;
      this.port.postMessage({ type: 'pcm16', buffer: buf }, [buf]);

      // Kullanılan sample'ları buffer'dan at
      this._buffer.copyWithin(0, sourceChunkSize, this._bufferLen);
      this._bufferLen -= sourceChunkSize;
    }

    return true; // Processor'ı canlı tut
  }
}

registerProcessor('audio-processor', AudioProcessor);
