import { useEffect, useId, useMemo, useState } from 'react';
import { FigureRenderAsset } from '../../types';

type PreviewFigureProps = {
  figure?: FigureRenderAsset;
  figureId: string;
};

type RenderState =
  | { status: 'idle' | 'rendering' }
  | { status: 'success'; svg: string }
  | { status: 'failed'; message: string };

let mermaidConfigured = false;
let mermaidModule: typeof import('mermaid').default | null = null;

async function getMermaid() {
  if (!mermaidModule) {
    mermaidModule = (await import('mermaid')).default;
  }
  if (mermaidConfigured) {
    return mermaidModule;
  }
  mermaidModule.initialize({
    startOnLoad: false,
    securityLevel: 'strict',
    theme: 'base',
    themeVariables: {
      background: '#fffdf8',
      primaryColor: '#fbf7ef',
      primaryBorderColor: '#c9b99d',
      primaryTextColor: '#1f2933',
      lineColor: '#6b7280',
      fontFamily: '-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif',
    },
    flowchart: {
      curve: 'basis',
      htmlLabels: false,
      nodeSpacing: 34,
      rankSpacing: 42,
    },
  });
  mermaidConfigured = true;
  return mermaidModule;
}

export function PreviewFigure({ figure, figureId }: PreviewFigureProps) {
  const reactId = useId();
  const renderId = useMemo(() => `preview_figure_${figureId}_${reactId.replace(/[^a-zA-Z0-9_]/g, '_')}`, [figureId, reactId]);
  const mermaidSource = figure?.source?.type === 'mermaid' ? figure.source.content.trim() : '';
  const [state, setState] = useState<RenderState>({ status: 'idle' });
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    if (!mermaidSource) {
      setState({ status: 'failed', message: '附图源码缺失' });
      return;
    }

    setState({ status: 'rendering' });
    getMermaid()
      .then((mermaid) => mermaid.render(renderId, mermaidSource))
      .then(({ svg }) => {
        if (!cancelled) {
          setState({ status: 'success', svg });
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setState({
            status: 'failed',
            message: error instanceof Error ? error.message : 'Mermaid 渲染失败',
          });
        }
      });

    return () => {
      cancelled = true;
    };
  }, [mermaidSource, renderId]);

  useEffect(() => {
    if (!expanded) {
      return;
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setExpanded(false);
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [expanded]);

  if (!figure) {
    return <div className="preview-figure-missing">附图未生成</div>;
  }
  if (state.status === 'success') {
    return (
      <>
        <div className="preview-figure-svg">
          <button
            type="button"
            className="preview-figure-expand"
            aria-label="展开预览附图"
            title="展开预览"
            onClick={() => setExpanded(true)}
          >
            <svg viewBox="0 0 16 16" aria-hidden="true">
              <path d="M5.6 2H2v3.6M10.4 2H14v3.6M14 10.4V14h-3.6M2 10.4V14h3.6" />
              <path d="M2.4 2.4 6 6M13.6 2.4 10 6M13.6 13.6 10 10M2.4 13.6 6 10" />
            </svg>
          </button>
          <div className="preview-figure-svg-content" dangerouslySetInnerHTML={{ __html: state.svg }} />
        </div>
        {expanded ? (
          <div className="preview-figure-lightbox" role="dialog" aria-modal="true" aria-label="附图预览" onClick={() => setExpanded(false)}>
            <div className="preview-figure-lightbox-panel" onClick={(event) => event.stopPropagation()}>
              <div className="preview-figure-lightbox-toolbar">
                <span>{figure.label} {figure.title}</span>
                <button type="button" className="preview-figure-lightbox-close" aria-label="关闭附图预览" onClick={() => setExpanded(false)}>
                  <svg viewBox="0 0 16 16" aria-hidden="true">
                    <path d="m4 4 8 8M12 4l-8 8" />
                  </svg>
                </button>
              </div>
              <div className="preview-figure-lightbox-body" dangerouslySetInnerHTML={{ __html: state.svg }} />
            </div>
          </div>
        ) : null}
      </>
    );
  }
  if (state.status === 'failed') {
    return <div className="preview-figure-missing">{state.message}</div>;
  }
  return <div className="preview-figure-missing">附图渲染中</div>;
}
