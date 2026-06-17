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

  if (!figure) {
    return <div className="preview-figure-missing">附图未生成</div>;
  }
  if (state.status === 'success') {
    return <div className="preview-figure-svg" dangerouslySetInnerHTML={{ __html: state.svg }} />;
  }
  if (state.status === 'failed') {
    return <div className="preview-figure-missing">{state.message}</div>;
  }
  return <div className="preview-figure-missing">附图渲染中</div>;
}
