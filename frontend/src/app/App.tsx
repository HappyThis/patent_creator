import { useRef } from 'react';
import { ChatPanel } from '../features/chat/ChatPanel';
import { OutlinePanel } from '../features/outline/OutlinePanel';
import { PreviewPanel } from '../features/preview/PreviewPanel';
import { useDemoWorkspace } from '../hooks/useDemoWorkspace';
import { Topbar } from './layout/Topbar';

function App() {
  const previewRef = useRef<HTMLDivElement | null>(null);
  const {
    project,
    renderAst,
    chat,
    timeline,
    composer,
    activeSectionId,
    activeBlockId,
    recentSectionIds,
    recentBlockIds,
    setComposer,
    setActiveSectionId,
    setActiveBlockId,
    simulateRound,
  } = useDemoWorkspace();

  return (
    <div className="app-shell">
      <Topbar project={project} />

      <main className="workspace">
        <OutlinePanel
          outline={renderAst.outline}
          activeSectionId={activeSectionId}
          recentSectionIds={recentSectionIds}
          onSelect={(sectionId) => {
            setActiveSectionId(sectionId);
            setActiveBlockId(null);
          }}
        />

        <PreviewPanel
          renderAst={renderAst}
          activeSectionId={activeSectionId}
          activeBlockId={activeBlockId}
          recentSectionIds={recentSectionIds}
          recentBlockIds={recentBlockIds}
          previewRef={previewRef}
        />

        <ChatPanel
          messages={chat}
          timeline={timeline}
          composer={composer}
          isBusy={project.isBusy}
          onComposerChange={setComposer}
          onSubmit={simulateRound}
        />
      </main>
    </div>
  );
}

export default App;
