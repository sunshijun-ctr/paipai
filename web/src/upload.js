/* Composer file/image uploads: the 📎 document button, the 🖼 image button,
 * and clipboard image paste. Documents go to /api/upload (indexed for RAG);
 * images go to /api/image/upload and become the pending image attached to
 * the next chat turn.
 *
 * Uses multipart FormData, so these stay on raw fetch (the api.js wrappers
 * are JSON-only). Dependencies via imports where clean; session state via
 * window (currentSid / chatStarted), owned by session-list.js.
 */

import { toast } from "./utils.js";
import { updateDownloaded } from "./papers.js";
import { startChat } from "./session-list.js";
import { addMsg, setPendingImage } from "./chat.js";

export async function uploadFile(input) {
  const file = input.files[0];
  if (!file || !window.currentSid) return;
  input.value = '';

  toast(`正在上传 ${file.name}…`);
  const form = new FormData();
  form.append('file', file);

  try {
    const r = await fetch(`/api/upload?session_id=${encodeURIComponent(window.currentSid)}`,
                          {method:'POST', body:form, credentials:'include'});
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      toast(`上传失败：${e.detail || r.statusText}`);
      return;
    }
    const d = await r.json();
    updateDownloaded(d.stored_papers || []);
    if (!window.chatStarted) startChat();
    toast(`✓ 已上传：${file.name}`);
    addMsg('assistant', `文档 **${file.name}** 已上传，可以直接提问了。`, 'general_chat');
  } catch(err) {
    toast(`上传出错：${err.message}`);
  }
}

export async function uploadImage(input) {
  const file = input.files[0];
  if (!file) return;
  input.value = '';
  await uploadImageFile(file);
}

export async function uploadImageFile(file) {
  if (!file || !window.currentSid) return;
  if (!file.type?.startsWith('image/')) {
    toast('剪贴板内容不是图片');
    return;
  }

  const name = file.name || '粘贴的图片';
  toast(`正在上传图片 ${name}…`);
  const form = new FormData();
  form.append('file', file, file.name || `pasted-image-${Date.now()}.png`);

  try {
    const r = await fetch(`/api/image/upload?session_id=${encodeURIComponent(window.currentSid)}`,
                          {method:'POST', body:form, credentials:'include'});
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      toast(`图片上传失败：${e.detail || r.statusText}`);
      return;
    }
    const d = await r.json();
    setPendingImage(d);
    if (!window.chatStarted) startChat();
    toast(`✓ 已上传图片：${name}`);
    addMsg('assistant', `图片 **${name}** 已上传。请发送你想问的问题，我会先解析图片内容。`, 'general_chat');
  } catch(err) {
    toast(`图片上传出错：${err.message}`);
  }
}

export async function handleImagePaste(event) {
  const items = Array.from(event.clipboardData?.items || []);
  const item = items.find(x => x.kind === 'file' && x.type.startsWith('image/'));
  if (!item) return;
  const file = item.getAsFile();
  if (!file) return;
  event.preventDefault();
  await uploadImageFile(file);
}
