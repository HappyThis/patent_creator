import { useCallback, useEffect, useRef, useState } from 'react';
import { apiClient } from '../../services/api/client';

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

const DRAWIO_EMBED_URL =
  'https://embed.diagrams.net/?embed=1&proto=json&spin=1&ui=min&libraries=1&noExitBtn=1&noSaveBtn=1&saveAndExit=0';
const DRAWIO_ORIGIN = new URL(DRAWIO_EMBED_URL).origin;

export function FigureDrawioEditorModal({ projectId, figureId, onClose, onSaved }: FigureDrawioEditorModalProps) {
  const frameRef = useRef<HTMLIFrameElement | null>(null);
  const latestXmlRef = useRef('');
  const loadedIntoEditorRef = useRef(false);
  const pendingXmlRequestRef = useRef<PendingXmlRequest | null>(null);
  const [title, setTitle] = useState('');
  const [drawioXml, setDrawioXml] = useState('');
  const [drawioUpdatedAt, setDrawioUpdatedAt] = useState('');
  const [loading, setLoading] = useState(true);
  const [iframeReady, setIframeReady] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const postToDrawio = useCallback((message: Record<string, unknown>) => {
    const target = frameRef.current?.contentWindow;
    if (!target) {
      return;
    }
    target.postMessage(JSON.stringify(message), DRAWIO_ORIGIN);
  }, []);

  const loadIntoEditor = useCallback(() => {
    if (!latestXmlRef.current) {
      return;
    }
    postToDrawio({
      action: 'load',
      autosave: 1,
      modified: 'unsavedChanges',
      noExitBtn: 1,
      noSaveBtn: 1,
      saveAndExit: '0',
      title: title || '未命名附图',
      xml: latestXmlRef.current,
    });
  }, [postToDrawio, title]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    apiClient
      .getFigureDrawio(projectId, figureId)
      .then((response) => {
        if (cancelled) {
          return;
        }
        const figure = response.figure;
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
      if (event.origin !== DRAWIO_ORIGIN || event.source !== frameRef.current?.contentWindow || !event.data) {
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
  }, []);

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
      setDirty(false);
      onSaved();
      onClose();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }, [drawioUpdatedAt, figureId, onClose, onSaved, projectId, requestCurrentXml, title]);

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
            <button type="button" className="figure-editor-secondary" onClick={handleClose} disabled={saving}>
              取消
            </button>
            <button type="button" className="figure-editor-primary" onClick={handleSave} disabled={saving || loading || !iframeReady}>
              {saving ? '保存中' : '保存'}
            </button>
          </div>
        </div>
        {error ? <div className="figure-editor-error">{error}</div> : null}
        <div className="figure-editor-main figure-editor-main-drawio">
          <iframe
            ref={frameRef}
            className="figure-editor-drawio-frame"
            title="draw.io 附图编辑器"
            src={DRAWIO_EMBED_URL}
          />
          {loading ? <div className="figure-editor-loading">正在加载 draw.io 附图...</div> : null}
        </div>
      </div>
    </div>
  );
}
