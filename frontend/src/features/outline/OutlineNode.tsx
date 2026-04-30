import type { ReactNode } from 'react';
import { OutlineItem } from '../../types';
import { SectionStatus } from '../preview/documentStats';

type OutlineNodeProps = {
  item: OutlineItem;
  indexPath: number[];
  activeSectionId: string;
  recentSectionIds: string[];
  sectionStatusById: Record<string, SectionStatus>;
  onSelect: (sectionId: string) => void;
};

export function OutlineNode({
  item,
  indexPath,
  activeSectionId,
  recentSectionIds,
  sectionStatusById,
  onSelect,
}: OutlineNodeProps) {
  const isActive = activeSectionId === item.id;
  const isRecent = recentSectionIds.includes(item.id);
  const sectionStatus = sectionStatusById[item.id];
  const sectionNumber = indexPath.join('.');
  const statusLabel = sectionStatus?.filled ? '已填写' : '待补充';

  return (
    <div className="outline-node">
      <button
        className={`outline-button level-${item.level} ${isActive ? 'active' : ''} ${isRecent ? 'recent' : ''}`}
        onClick={() => onSelect(item.id)}
        title={`${sectionNumber}. ${item.title}`}
      >
        <span className="outline-drag-handle" aria-hidden="true" />
        <span className="outline-icon" aria-hidden="true">{getOutlineIcon(item.id)}</span>
        <span className="outline-copy">
          <span className="outline-number">{sectionNumber}.</span>
          <span className="outline-label">{item.title}</span>
        </span>
        <span className={`outline-status ${sectionStatus?.filled ? 'filled' : 'empty'}`} aria-label={statusLabel} title={statusLabel} />
        <span className="outline-action" aria-hidden="true">›</span>
      </button>
      {item.children?.length ? (
        <div className="outline-children">
          {item.children.map((child, index) => (
            <OutlineNode
              key={child.id}
              item={child}
              indexPath={[...indexPath, index + 1]}
              activeSectionId={activeSectionId}
              recentSectionIds={recentSectionIds}
              sectionStatusById={sectionStatusById}
              onSelect={onSelect}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function getOutlineIcon(sectionId: string): JSX.Element {
  switch (sectionId) {
    case 'title':
      return (
        <IconSvg>
          <path d="M5.5 15.5h7" />
          <path d="M6.5 3.5h5l2 2v11h-7a2 2 0 0 1-2-2v-9a2 2 0 0 1 2-2Z" />
          <path d="M11.5 3.5v3h3" />
          <path d="M8 11.5l4-4 1.5 1.5-4 4-2 .5.5-2Z" />
        </IconSvg>
      );
    case 'technical_field':
      return (
        <IconSvg>
          <circle cx="10" cy="10" r="6.5" />
          <circle cx="10" cy="10" r="3" />
          <circle cx="10" cy="10" r="0.7" fill="currentColor" />
        </IconSvg>
      );
    case 'background_technology':
      return (
        <IconSvg>
          <path d="M4 7.5 10 4l6 3.5-6 3.5-6-3.5Z" />
          <path d="M4 11.5 10 15l6-3.5" />
          <path d="M4 9.5 10 13l6-3.5" />
        </IconSvg>
      );
    case 'existing_solution':
      return (
        <IconSvg>
          <path d="M4 8h12" />
          <path d="M5.5 12h9" />
          <path d="M7 5h8l-2 10H5L7 5Z" />
        </IconSvg>
      );
    case 'existing_solution_defects':
      return (
        <IconSvg>
          <path d="M10 4 17 16H3L10 4Z" />
          <path d="M10 8v4" />
          <path d="M10 14.8v.2" />
        </IconSvg>
      );
    case 'technical_problem':
      return (
        <IconSvg>
          <circle cx="10" cy="10" r="7" />
          <path d="M7.8 8a2.4 2.4 0 0 1 4.4 1.3c0 1.7-1.8 2-2.2 3.2" />
          <path d="M10 15.5v.2" />
        </IconSvg>
      );
    case 'technical_solution':
      return (
        <IconSvg>
          <path d="m12.2 4.5 3.3 3.3" />
          <path d="M7.2 14.7 4.5 17.4l-2-2 2.7-2.7" />
          <path d="M6.5 8.5 3.8 5.8l2-2 2.7 2.7" />
          <path d="M15 4.2 4.2 15" />
        </IconSvg>
      );
    case 'key_innovations':
      return (
        <IconSvg>
          <path d="M10 3.5 11.9 8l4.8.4-3.7 3.1 1.1 4.7L10 13.7l-4.1 2.5L7 11.5 3.3 8.4 8.1 8 10 3.5Z" />
        </IconSvg>
      );
    case 'embodiments':
      return (
        <IconSvg>
          <path d="M5.5 4.5h9v11h-9z" />
          <path d="M7.5 7.5h5" />
          <path d="M7.5 10h5" />
          <path d="M7.5 12.5h3.5" />
        </IconSvg>
      );
    case 'technical_effects':
      return (
        <IconSvg>
          <path d="m4.5 10.5 3.5 3.5 7.5-8" />
          <path d="M15.5 10A5.5 5.5 0 1 1 10 4.5" />
        </IconSvg>
      );
    case 'drawings':
      return (
        <IconSvg>
          <rect x="4.5" y="5" width="11" height="10" rx="1.2" />
          <circle cx="8" cy="8.2" r="1" />
          <path d="m5.8 13 3.1-3 2.3 2.1 1.5-1.4 2.8 2.9" />
        </IconSvg>
      );
    case 'claim_suggestions':
      return (
        <IconSvg>
          <path d="M10 3.8 15 6v4.2c0 3.1-1.9 5.1-5 6-3.1-.9-5-2.9-5-6V6l5-2.2Z" />
          <path d="m7.5 10.2 1.7 1.7 3.4-3.6" />
        </IconSvg>
      );
    default:
      return (
        <IconSvg>
          <path d="M5 5h10v10H5z" />
        </IconSvg>
      );
  }
}

function IconSvg({ children }: { children: ReactNode }) {
  return (
    <svg
      className="outline-icon-svg"
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="1.45"
      aria-hidden="true"
    >
      {children}
    </svg>
  );
}
