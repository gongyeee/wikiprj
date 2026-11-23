export async function readApiResponse(response) {
  let data;
  try {
    data = response.status === 204 ? {} : JSON.parse(await response.text());
  } catch {
    const messages = {
      401: '登录已失效，请刷新页面后重新登录。',
      403: '登录校验未通过，请刷新页面后重试。',
      404: '接口尚未加载（404），请重启后端服务后重试。',
      413: '上传请求超过服务器大小限制，请改用分批上传或服务端本地目录摄入。',
      500: '服务器处理上传或读取目录失败，请检查服务日志与存储权限。',
      502: '暂时无法连接后端服务，请稍后重试。',
      503: '后端服务暂不可用，请稍后重试。'
    };
    const error = new Error(messages[response.status] || `接口返回了非 JSON 内容（${response.status}），请检查后端服务与接口地址。`);
    error.status = response.status;
    throw error;
  }
  if (!response.ok) {
    const error = new Error(data?.error || data?.detail || `请求失败（${response.status}）`);
    error.status = response.status;
    error.payload = data;
    throw error;
  }
  return data;
}
