import test from 'node:test';
import assert from 'node:assert/strict';
import { readApiResponse } from '../src/apiResponse.js';

test('HTML 404 显示接口未加载提示，不泄露 JSON 解析异常或错误页正文', async () => {
  const response = new Response('<!DOCTYPE html><html>Page not found</html>', { status: 404 });
  await assert.rejects(readApiResponse(response), error => {
    assert.equal(error.status, 404);
    assert.match(error.message, /接口尚未加载.*重启后端/);
    assert.doesNotMatch(error.message, /Unexpected token|DOCTYPE/);
    return true;
  });
});
test('代理误返回 HTML 200 也不能当作接口成功', async () => {
  await assert.rejects(readApiResponse(new Response('<!DOCTYPE html>', { status: 200 })), /非 JSON/);
});
test('正常 JSON、业务错误和登录状态保留', async () => {
  assert.deepEqual(await readApiResponse(new Response('{"files":[]}')), { files: [] });
  await assert.rejects(readApiResponse(new Response('{"error":"请先登录","needLogin":true}', { status: 401 })), error => {
    assert.equal(error.status, 401);
    assert.equal(error.payload.needLogin, true);
    assert.equal(error.message, '请先登录');
    return true;
  });
});
