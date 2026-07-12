import { useCallback, useEffect, useRef, useState } from 'react';
import { apiClient } from '../../services/api/client';
import { ApiRequestError } from '../../services/api/http';

type FigureDrawioEditorModalProps = {
  projectId: string;
  figureId: string;
  onClose: () => void;
  onSaved: () => void;
};

type PendingXmlRequest = {
  resolve: (xml: string) => void;
  reject: (error: Error) => void;
  timer: number;
};

export function FigureDrawioEditorModal({ projectId, figureId, onClose, onSaved }: FigureDrawioEditorModalProps) {
  const frameRef = useRef<HTMLIFrameElement | null>(null);
  const latestXmlRef = useRef('');
  const loadedIntoEditorRef = useRef(false);
  const pendingXmlRequestRef = useRef<PendingXmlRequest | null>(null);
  const [title, setTitle] = useState('');
  const [drawioXml, setDrawioXml] = useState('');
  const [drawioUpdatedAt, setDrawioUpdatedAt] = useState('');
  const [drawioEmbedUrl, setDrawioEmbedUrl] = useState('');
  const [drawioOrigin, setDrawioOrigin] = useState('');
  const [loading, setLoading] = useState(true);
  const [iframeReady, setIframeReady] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [downloadingLocal, setDownloadingLocal] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);

  const postToDrawio = useCallback((message: Record<string, unknown>) => {
    const target = frameRef.current?.contentWindow;
    if (!target || !drawioOrigin) {
      return;
    }
    target.postMessage(JSON.stringify(message), drawioOrigin);
  }, [drawioOrigin]);

  const postLoad = useCallback((xml: string, nextTitle: string) => {
    postToDrawio({
      action: 'load',
      autosave: 1,
      modified: 'unsavedChanges',
      noExitBtn: 1,
      noSaveBtn: 1,
      saveAndExit: '0',
      title: nextTitle || '未命名附图',
      xml,
    });
  }, [postToDrawio]);

  const loadIntoEditor = useCallback(() => {
    if (!latestXmlRef.current) {
      return;
    }
    postLoad(latestXmlRef.current, title);
  }, [postLoad, title]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setConflict(false);
    setIframeReady(false);
    Promise.all([apiClient.getRuntimeConfig(), apiClient.getFigureDrawio(projectId, figureId)])
      .then(([config, response]) => {
        if (cancelled) {
          return;
        }
        const embedUrl = config.drawio_embed_url;
        if (!embedUrl) {
          throw new Error('后端未配置 Draw.io 服务。');
        }
        const figure = response.figure;
        setDrawioEmbedUrl(embedUrl);
        setDrawioOrigin(new URL(embedUrl).origin);
        latestXmlRef.current = figure.drawio_xml;
        loadedIntoEditorRef.current = false;
        setDrawioXml(figure.drawio_xml);
        setTitle(figure.title);
        setDrawioUpdatedAt(figure.drawio_updated_at);
        setDirty(false);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [figureId, projectId]);

  useEffect(() => {
    if (!iframeReady || !drawioXml || loadedIntoEditorRef.current) {
      return;
    }
    loadedIntoEditorRef.current = true;
    loadIntoEditor();
  }, [drawioXml, iframeReady, loadIntoEditor]);

  useEffect(() => {
    function handleMessage(event: MessageEvent) {
      if (!drawioOrigin || event.origin !== drawioOrigin || event.source !== frameRef.current?.contentWindow || !event.data) {
        return;
      }
      let message: Record<string, unknown>;
      try {
        message = JSON.parse(String(event.data)) as Record<string, unknown>;
      } catch {
        return;
      }
      if (message.error) {
        const pending = pendingXmlRequestRef.current;
        if (pending) {
          window.clearTimeout(pending.timer);
          pendingXmlRequestRef.current = null;
          pending.reject(new Error(String(message.error)));
        } else {
          setError(String(message.error));
        }
        return;
      }
      const eventName = typeof message.event === 'string' ? message.event : '';
      if (eventName === 'init') {
        setIframeReady(true);
        return;
      }
      if (eventName === 'load') {
        setError(null);
        return;
      }
      if (eventName === 'autosave' || eventName === 'save') {
        const xml = typeof message.xml === 'string' ? message.xml : '';
        if (xml) {
          latestXmlRef.current = xml;
          setDrawioXml(xml);
          setDirty(true);
        }
        return;
      }
      if (eventName === 'export') {
        const pending = pendingXmlRequestRef.current;
        if (!pending) {
          return;
        }
        const xml = typeof message.xml === 'string' ? message.xml : typeof message.data === 'string' ? message.data : '';
        window.clearTimeout(pending.timer);
        pendingXmlRequestRef.current = null;
        if (!xml || !xml.trim().startsWith('<')) {
          pending.reject(new Error('draw.io 没有返回 XML。'));
          return;
        }
        latestXmlRef.current = xml;
        setDrawioXml(xml);
        pending.resolve(xml);
      }
    }

    window.addEventListener('message', handleMessage);
    return () => window.removeEventListener('message', handleMessage);
  }, [drawioOrigin]);

  useEffect(() => {
    if (!drawioEmbedUrl || iframeReady) {
      return;
    }
    const timer = window.setTimeout(() => {
      setError('Draw.io 编辑器连接超时，请确认本地 Draw.io 服务已经启动。');
    }, 15000);
    return () => window.clearTimeout(timer);
  }, [drawioEmbedUrl, iframeReady]);

  useEffect(() => {
    return () => {
      const pending = pendingXmlRequestRef.current;
      if (pending) {
        window.clearTimeout(pending.timer);
        pending.reject(new Error('编辑器已关闭。'));
      }
    };
  }, []);

  const requestCurrentXml = useCallback(() => {
    if (!iframeReady) {
      return Promise.resolve(latestXmlRef.current);
    }
    const existing = pendingXmlRequestRef.current;
    if (existing) {
      window.clearTimeout(existing.timer);
      existing.reject(new Error('新的 XML 请求已开始。'));
    }
    return new Promise<string>((resolve, reject) => {
      const timer = window.setTimeout(() => {
        pendingXmlRequestRef.current = null;
        reject(new Error('获取 draw.io XML 超时。'));
      }, 12000);
      pendingXmlRequestRef.current = { resolve, reject, timer };
      postToDrawio({ action: 'export', format: 'xml' });
    });
  }, [iframeReady, postToDrawio]);

  const handleClose = useCallback(() => {
    if (dirty && !window.confirm('当前附图尚未保存，确定关闭吗？')) {
      return;
    }
    onClose();
  }, [dirty, onClose]);

  const handleSave = useCallback(async () => {
    if (!drawioUpdatedAt) {
      setError('缺少 drawio_updated_at，请关闭后重新打开。');
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const currentXml = await requestCurrentXml();
      const response = await apiClient.saveFigureDrawio(projectId, figureId, {
        title,
        drawio_xml: currentXml,
        expected_drawio_updated_at: drawioUpdatedAt,
      });
      setDrawioUpdatedAt(response.figure.drawio_updated_at);
      setConflict(false);
      setDirty(false);
      onSaved();
      onClose();
    } catch (err: unknown) {
      if (err instanceof ApiRequestError && err.code === 'drawio_conflict') {
        setConflict(true);
      }
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }, [drawioUpdatedAt, figureId, onClose, onSaved, projectId, requestCurrentXml, title]);

  const handleReloadLatest = useCallback(async () => {
    if (!window.confirm('加载最新版本会放弃当前未保存的修改，确定继续吗？')) {
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const response = await apiClient.getFigureDrawio(projectId, figureId);
      const figure = response.figure;
      latestXmlRef.current = figure.drawio_xml;
      setDrawioXml(figure.drawio_xml);
      setTitle(figure.title);
      setDrawioUpdatedAt(figure.drawio_updated_at);
      setDirty(false);
      setConflict(false);
      if (iframeReady) {
        loadedIntoEditorRef.current = true;
        postLoad(figure.drawio_xml, figure.title);
      } else {
        loadedIntoEditorRef.current = false;
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [figureId, iframeReady, postLoad, projectId]);

  const handleDownloadLocalCopy = useCallback(async () => {
    setDownloadingLocal(true);
    try {
      const currentXml = await requestCurrentXml();
      const downloadUrl = URL.createObjectURL(new Blob([currentXml], { type: 'application/xml;charset=utf-8' }));
      const link = document.createElement('a');
      link.href = downloadUrl;
      link.download = localCopyFilename(title, figureId);
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(downloadUrl), 0);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setDownloadingLocal(false);
    }
  }, [figureId, requestCurrentXml, title]);

  return (
    <div className="figure-editor-backdrop" role="dialog" aria-modal="true" aria-label="编辑附图">
      <div className="figure-editor-modal">
        <div className="figure-editor-header">
          <div className="figure-editor-titlebar">
            <input
              className="figure-editor-title-input"
              aria-label="附图标题"
              value={title}
              onChange={(event) => {
                setTitle(event.target.value);
                setDirty(true);
              }}
              placeholder="附图标题"
            />
          </div>
          <div className="figure-editor-actions">
            <button type="button" className="figure-editor-secondary" onClick={handleClose} disabled={saving || downloadingLocal}>
              取消
            </button>
            <button
              type="button"
              className="figure-editor-primary"
              onClick={handleSave}
              disabled={saving || downloadingLocal || loading || !iframeReady}
            >
              {saving ? '保存中' : '保存'}
            </button>
          </div>
        </div>
        {error ? (
          <div className="figure-editor-error">
            <span>{error}</span>
            {conflict ? (
              <div className="figure-editor-error-actions">
                <button type="button" onClick={handleDownloadLocalCopy} disabled={loading || saving || downloadingLocal}>
                  {downloadingLocal ? '正在导出' : '下载本地副本'}
                </button>
                <button type="button" onClick={handleReloadLatest} disabled={loading || saving || downloadingLocal}>
                  加载最新版本
                </button>
              </div>
            ) : null}
          </div>
        ) : null}
        <div className="figure-editor-main figure-editor-main-drawio">
          {drawioEmbedUrl ? (
            <iframe
              ref={frameRef}
              className="figure-editor-drawio-frame"
              title="draw.io 附图编辑器"
              src={drawioEmbedUrl}
              sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-downloads"
              onError={() => setError('Draw.io 编辑器加载失败，请检查服务地址和运行状态。')}
            />
          ) : null}
          {loading ? <div className="figure-editor-loading">正在加载 draw.io 附图...</div> : null}
        </div>
      </div>
    </div>
  );
}

function localCopyFilename(title: string, figureId: string): string {
  const stem = (title.trim() || figureId)
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_')
    .replace(/[. ]+$/g, '')
    .slice(0, 80) || figureId;
  return `${stem}-本地冲突副本.drawio`;
}
