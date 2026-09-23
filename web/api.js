export class ApiError extends Error {
  constructor(message, status = 0, code = '') {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

export async function responseError(response) {
  let payload;
  try { payload = await response.json(); } catch { /* A proxy may return non-JSON. */ }
  return new ApiError(payload?.error?.message || `服务请求失败（${response.status}），请稍后重试。`, response.status, payload?.error?.code || '');
}

export async function api(path, { method = 'GET', body, signal } = {}) {
  let response;
  try {
    response = await fetch(`/api/admin/${path}`, {
      method, signal, cache: 'no-store', credentials: 'same-origin',
      headers: body === undefined ? { Accept: 'application/json' } : { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new ApiError('无法连接网关，请确认服务仍在运行。');
  }
  if (!response.ok) throw await responseError(response);
  try { return await response.json(); }
  catch { throw new ApiError('服务返回了无法识别的数据，请重试。'); }
}
