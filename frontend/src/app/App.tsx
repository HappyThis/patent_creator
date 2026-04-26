import { useRef } from 'react';
import { ChatPanel } from '../features/chat/ChatPanel';
import { OutlinePanel } from '../features/outline/OutlinePanel';
import { PreviewPanel } from '../features/preview/PreviewPanel';
import { useDemoWorkspace } from '../hooks/useDemoWorkspace';

function App() {
  const previewRef = useRef<HTMLDivElement | null>(null);
  const {
    renderAst,
    events,
    composer,
    isBusy,
    sessionTabs,
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
      <main className="workspace">
        <section className="document-workspace">
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
            onActiveSectionChange={setActiveSectionId}
          />
        </section>

        <ChatPanel
          sessionTabs={sessionTabs}
          events={events}
          composer={composer}
          isBusy={isBusy}
          onComposerChange={setComposer}
          onSubmit={simulateRound}
        />
      </main>
    </div>
  );
}

export default App;
