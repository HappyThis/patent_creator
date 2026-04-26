import { OutlineItem } from '../../types';
import { OutlineNode } from './OutlineNode';

type OutlinePanelProps = {
  outline: OutlineItem[];
  activeSectionId: string;
  recentSectionIds: string[];
  onSelect: (sectionId: string) => void;
};

export function OutlinePanel({
  outline,
  activeSectionId,
  recentSectionIds,
  onSelect,
}: OutlinePanelProps) {
  return (
    <aside className="outline-pane">
      <div className="outline-list">
        {outline.map((item) => (
          <OutlineNode
            key={item.id}
            item={item}
            activeSectionId={activeSectionId}
            recentSectionIds={recentSectionIds}
            onSelect={onSelect}
          />
        ))}
      </div>
    </aside>
  );
}
