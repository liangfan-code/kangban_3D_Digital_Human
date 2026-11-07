/**
 * 康伴 Day3 语音测试台 · 采集处理器（仅声纹录入用）。
 *
 * 主对话链路音频走 WebRTC 轨（无需本文件）；本 Worklet 只在
 * 「录入声纹」时被注册：输出原始 Float32 采样块，由主线程
 * 重采样 16k → int16 → base64 后 POST /voice/enroll/device。
 *
 * 参考来源：教师样板 C:/Users/yang/Desktop/asr/src/asr/web 下的采集写法。
 */
class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._chunk = [];
    this._chunkSamples = 0;
    this._targetSamples = 4800; // 100ms @48kHz 一块
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    // 取第一通道（mono）
    const ch = input[0];
    for (let i = 0; i < ch.length; i++) {
      this._chunk.push(ch[i]);
      this._chunkSamples++;
      if (this._chunkSamples >= this._targetSamples) {
        // transferable：转出副本，避免跨线程共享
        this.port.postMessage(Float32Array.from(this._chunk));
        this._chunk = [];
        this._chunkSamples = 0;
      }
    }
    return true;
  }
}

registerProcessor("capture-processor", CaptureProcessor);
