export async function forwardOutbound(request, network, timeoutMs = 12_000) {
  const signal = AbortSignal.any([request.signal, AbortSignal.timeout(timeoutMs)]);
  const result = await network(request.url, {
    method: request.method, headers: Object.fromEntries(request.headers), signal,
    ...(request.method === 'GET' || request.method === 'HEAD' ? {} : { body: await request.arrayBuffer() }),
  });
  return new Response(result.body, { status: result.status, headers: Object.fromEntries(result.headers) });
}

// Timeout does not launch another read while the underlying read is pending.
export function singleFlightReader(read, timeoutMs = 8_000) {
  let flight;
  return async () => {
    if (!flight) flight = Promise.resolve().then(read).finally(() => { flight = undefined; });
    let timer;
    try {
      return await Promise.race([flight, new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error('监控数据读取超时，等待服务恢复')), timeoutMs);
      })]);
    } finally { clearTimeout(timer); }
  };
}
