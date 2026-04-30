import { OutlineItem } from '../../types';
import { SectionStatus } from '../preview/documentStats';
import { OutlineNode } from './OutlineNode';

type OutlinePanelProps = {
  outline: OutlineItem[];
  activeSectionId: string;
  recentSectionIds: string[];
  sectionStatusById: Record<string, SectionStatus>;
  onSelect: (sectionId: string) => void;
};

export function OutlinePanel({
  outline,
  activeSectionId,
  recentSectionIds,
  sectionStatusById,
  onSelect,
}: OutlinePanelProps) {
  const totalSections = outline.length;
  const filledSections = outline.filter((item) => sectionStatusById[item.id]?.filled).length;
  const progressPercent = totalSections > 0 ? Math.round((filledSections / totalSections) * 100) : 0;

  return (
    <aside className="outline-pane">
      <div className="outline-list">
        <header className="outline-header">
          <div>
            <div className="outline-title">交底书目录</div>
            <div className="outline-subtitle">{filledSections}/{totalSections} 章节已填写</div>
          </div>
          <span className="outline-progress-value">{progressPercent}%</span>
        </header>
        <div className="outline-progress-track" aria-hidden="true">
          <span style={{ width: `${progressPercent}%` }} />
        </div>
        {outline.map((item, index) => (
          <OutlineNode
            key={item.id}
            item={item}
            indexPath={[index + 1]}
            activeSectionId={activeSectionId}
            recentSectionIds={recentSectionIds}
            sectionStatusById={sectionStatusById}
            onSelect={onSelect}
          />
        ))}
      </div>
    </aside>
  );
}
