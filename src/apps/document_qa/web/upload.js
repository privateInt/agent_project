'use strict';
// LAN HTTP has getRandomValues but may lack SubtleCrypto. Hashing stays local.
window.QAUpload = (() => {
  const MAX_BYTES = 32 * 1024 * 1024;
  const uid = value => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value);
  function uuid() {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    return [...bytes].map((n, i) => ([4, 6, 8, 10].includes(i) ? '-' : '') + n.toString(16).padStart(2, '0')).join('');
  }
  const K = [0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2];
  const rotate = (n, bits) => (n >>> bits) | (n << (32 - bits));
  async function sha256(input) {
    if (!(input instanceof Uint8Array) || input.length > MAX_BYTES) throw Error('FILE_INVALID');
    const length = Math.ceil((input.length + 9) / 64) * 64;
    const padded = new Uint8Array(length); padded.set(input); padded[input.length] = 128;
    new DataView(padded.buffer).setUint32(length - 4, input.length * 8);
    const hash = [0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19];
    const words = new Int32Array(64), view = new DataView(padded.buffer);
    for (let offset = 0; offset < length; offset += 64) {
      for (let i = 0; i < 16; i++) words[i] = view.getInt32(offset + i * 4);
      for (let i = 16; i < 64; i++) {
        const a = words[i - 15], b = words[i - 2];
        words[i] = (words[i - 16] + (rotate(a,7)^rotate(a,18)^(a>>>3)) + words[i - 7] + (rotate(b,17)^rotate(b,19)^(b>>>10))) | 0;
      }
      let [a,b,c,d,e,f,g,h] = hash;
      for (let i = 0; i < 64; i++) {
        const first = (h + (rotate(e,6)^rotate(e,11)^rotate(e,25)) + ((e&f)^(~e&g)) + K[i] + words[i]) | 0;
        const second = ((rotate(a,2)^rotate(a,13)^rotate(a,22)) + ((a&b)^(a&c)^(b&c))) | 0;
        h=g;g=f;f=e;e=(d+first)|0;d=c;c=b;b=a;a=(first+second)|0;
      }
      for (const [i, value] of [a,b,c,d,e,f,g,h].entries()) hash[i] = (hash[i] + value) | 0;
      if (offset && offset % (1 << 20) === 0) await new Promise(resolve => setTimeout(resolve, 0));
    }
    return hash.map(n => (n >>> 0).toString(16).padStart(8,'0')).join('');
  }
  function metadata(name, file) {
    name = name.trim();
    if (!name || name.length > 200 || /[\u0000-\u001f\u007f-\u009f]/.test(name)) throw Error('UPLOAD_NAME_INVALID');
    if (!file || !file.size || file.size > MAX_BYTES) throw Error('UPLOAD_BYTES_INVALID');
    if (!/\.(txt|md|markdown|pdf)$/i.test(file.name) || /[\\/:\u0000-\u001f\u007f-\u009f]/.test(file.name)
        || file.name.trim() !== file.name || new TextEncoder().encode(file.name).length > 255) throw Error('UPLOAD_FORMAT_UNSUPPORTED');
    return {name, filename:file.name, size_bytes:file.size};
  }
  function pending(value) {
    if (!value || Object.keys(value).sort().join(',') !== 'filename,key,library_id,name,schema,sha256,size_bytes'
        || value.schema !== 1 || !uid(value.key) || !uid(value.library_id) || !/^[0-9a-f]{64}$/.test(value.sha256)) throw Error('PENDING_RECORD_INVALID');
    const meta = metadata(value.name, {name:value.filename, size:value.size_bytes});
    if (!Number.isInteger(value.size_bytes) || meta.name !== value.name) throw Error('PENDING_RECORD_INVALID');
    return value;
  }
  function matches(record, value) {
    return ['name','filename','size_bytes','sha256'].every(key => record[key] === value[key]);
  }
  return {MAX_BYTES, uid, uuid, sha256, metadata, pending, matches};
})();
