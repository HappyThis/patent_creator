export type OutlineItem = {
  id: string;
  title: string;
  level: number;
  anchor: string;
  children?: OutlineItem[];
};

export type ParagraphNode = {
  type: 'paragraph';
  id: string;
  section_id: string;
  text: string;
};

export type ListNode = {
  type: 'list';
  id: string;
  section_id: string;
  ordered: boolean;
  items: string[];
};

export type ImageNode = {
  type: 'image';
  id: string;
  section_id: string;
  src: string;
  caption?: string;
  alt?: string;
};

export type TableNode = {
  type: 'table';
  id: string;
  section_id: string;
  columns: string[];
  rows: string[][];
};

export type SectionNode = {
  type: 'section';
  id: string;
  title: string;
  level: number;
  anchor: string;
  children: RenderNode[];
};

export type RenderNode = ParagraphNode | ListNode | ImageNode | TableNode | SectionNode;

export type RenderAst = {
  type: 'document';
  title: string;
  meta: {
    document_type: string;
    schema_version: string;
  };
  outline: OutlineItem[];
  children: RenderNode[];
};
