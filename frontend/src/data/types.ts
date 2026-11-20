/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  // 动态表格还承载后端盖戳的嵌套元数据（_definition/_validation/_query_plan 等）
  [field: string]: string | number | boolean | Record<string, unknown> | null
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
  /** 领域校验错误码，如 DIM_MISSING / FACT_NOT_FOUND / GRANULARITY_INVALID */
  code?: string | null
  /** 结构化校验明细：不能上卷的维度、源事实版本等 */
  details?: Record<string, unknown> | null
  /** 提交失败时后端原样回显的输入，用于回填表单、保证不清空 */
  retained?: Record<string, string> | null
}

/** 汇总字段定义回查视图：登记页与星型查询入口共享同一份定义 */
export type SummaryDefinition = {
  entry_id: number
  汇总名称: string
  源事实表: string
  汇总维度: string
  汇总度量: string
  汇总粒度: string
  definition: {
    source_fact: string
    fact_version: number
    group_dims: string[]
    granularity: string
    checked_at: string
    legacy: boolean
  } | null
  validation: {
    code: string
    valid: boolean
    missing_dims: string[]
    source_fact: string
    fact_version: number
    fact_grain: string
    checked_at: string
  } | null
  applicable_from: string | null
  applicable_to: string | null
  status: string
}

/** 星型查询选源计划：实际数据来源、适用日期与回退时扫描的事实分区 */
export type QueryPlan = {
  query_id: number
  query_name: string
  requested_dims: string[]
  source_fact: string
  fact_version: number
  fact_grain: string
  selected_source: {
    type: 'summary' | 'fact'
    summary_id: number | null
    summary_name: string | null
    source_fact: string
    fact_version: number
    group_dims: string[]
    granularity: string
    exact_grain_match: boolean
    applicable_from: string | null
    applicable_to: string | null
    scanned_partitions: {
      partition_field: string | null
      mode: string
      partitions: string[]
      filter: string
    } | null
  }
  candidate_summaries: Array<Record<string, unknown>>
  rejected_summaries: Array<{
    agg_id: number
    agg_name: string
    reason: string
    missing_dims?: string[]
  }>
  match_note: string
  generated_sql: string
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}
