<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span>{{ row.status }}</span>
            <span
              v-if="meta.key === 'agg_summary' && rowValidation(row)"
              class="valid-badge"
              :class="rowValidation(row)?.valid ? 'valid-ok' : 'valid-bad'"
              :data-testid="`validation-${row.id}`"
            >
              {{ rowValidation(row)?.code === 'VALID' ? '定义有效' : `定义失效：${rowValidation(row)?.missing_dims?.join('、') || rowValidation(row)?.code}` }}
            </span>
          </td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button
              v-if="meta.key === 'agg_summary'"
              class="link"
              type="button"
              :data-testid="`view-definition-${row.id}`"
              @click="viewDefinition(row)"
            >
              回查定义
            </button>
            <button
              v-if="meta.key === 'star_query'"
              class="link"
              type="button"
              :data-testid="`query-plan-${row.id}`"
              @click="viewQueryPlan(row)"
            >
              查询选源
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <div v-if="validationError" class="validation-panel" data-testid="validation-error">
          <p class="validation-title">
            <span class="error-code">{{ validationError.code }}</span>
            {{ validationError.message }}
          </p>
          <ul v-if="validationError.missingDims.length" class="missing-list">
            <li v-for="dim in validationError.missingDims" :key="dim" class="missing-dim">
              不能上卷的维度：{{ dim }}
            </li>
          </ul>
          <p v-if="validationError.factVersion" class="validation-meta">
            源事实「{{ validationError.sourceFact }}」v{{ validationError.factVersion }}
            （粒度：{{ validationError.factGrain }}）
          </p>
          <p class="validation-hint">原始输入已保留，请移除失效分组字段后重新提交。</p>
        </div>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="closeCreate">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="definitionView" class="modal-backdrop" data-testid="definition-modal">
      <div class="modal">
        <h3>汇总字段定义（{{ definitionView.汇总名称 }}）</h3>
        <dl class="detail-list">
          <dt>源事实表</dt><dd>{{ definitionView.源事实表 }}（v{{ definitionView.definition?.fact_version }}）</dd>
          <dt>分组键</dt><dd>{{ definitionView.definition?.group_dims.join(' . ') }}</dd>
          <dt>汇总粒度</dt><dd>{{ definitionView.汇总粒度 }}</dd>
          <dt>校验状态</dt>
          <dd>
            <span :class="definitionView.validation?.valid ? 'valid-ok' : 'valid-bad'">
              {{ definitionView.validation?.code }}
            </span>
            <span v-if="definitionView.validation?.missing_dims?.length">
              （失效字段：{{ definitionView.validation.missing_dims.join('、') }}）
            </span>
          </dd>
          <dt>适用日期</dt>
          <dd>{{ definitionView.applicable_from }} 至 {{ definitionView.applicable_to ?? '至今' }}</dd>
        </dl>
        <div class="page-actions">
          <button class="btn primary" type="button" @click="definitionView = null">关闭</button>
        </div>
      </div>
    </div>

    <div v-if="queryPlan" class="modal-backdrop" data-testid="query-plan-modal">
      <div class="modal">
        <h3>星型查询选源（{{ queryPlan.query_name }}）</h3>
        <p class="validation-title">{{ queryPlan.match_note }}</p>
        <dl class="detail-list">
          <dt>实际数据来源</dt>
          <dd>
            <span :class="queryPlan.selected_source.type === 'summary' ? 'valid-ok' : 'valid-bad'">
              {{ queryPlan.selected_source.type === 'summary' ? '现成汇总' : '原事实回退' }}
            </span>
            {{ queryPlan.selected_source.summary_name || queryPlan.selected_source.source_fact }}
          </dd>
          <dt>源事实版本</dt><dd>v{{ queryPlan.selected_source.fact_version }}</dd>
          <dt>分组键</dt><dd>{{ queryPlan.selected_source.group_dims.join(' . ') }}</dd>
          <dt>适用日期</dt>
          <dd>{{ queryPlan.selected_source.applicable_from }} 至 {{ queryPlan.selected_source.applicable_to ?? '至今' }}</dd>
          <template v-if="queryPlan.selected_source.scanned_partitions">
            <dt>事实分区字段</dt><dd>{{ queryPlan.selected_source.scanned_partitions.partition_field || '无分区' }}</dd>
            <dt>扫描方式</dt>
            <dd>
              {{ queryPlan.selected_source.scanned_partitions.mode === 'partition_pruned' ? '分区裁剪' : '全量扫描' }}
              <span v-if="queryPlan.selected_source.scanned_partitions.partitions.length">
                ：{{ queryPlan.selected_source.scanned_partitions.partitions.join('，') }}
              </span>
            </dd>
          </template>
          <dt>被排除的汇总</dt>
          <dd>
            <ul v-if="queryPlan.rejected_summaries.length" class="missing-list">
              <li v-for="item in queryPlan.rejected_summaries" :key="`${item.agg_id}-${item.reason}`">
                {{ item.agg_name }}：{{ reasonText(item.reason) }}
                <span v-if="item.missing_dims?.length">（{{ item.missing_dims.join('、') }}）</span>
              </li>
            </ul>
            <span v-else>无</span>
          </dd>
          <dt>生成SQL</dt><dd><pre class="sql-block">{{ queryPlan.generated_sql }}</pre></dd>
        </dl>
        <div class="page-actions">
          <button class="btn primary" type="button" @click="queryPlan = null">关闭</button>
        </div>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  listEntries,
  loadQueryPlan,
  loadSummaryDefinition,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import type { EntryRow, QueryPlan, SummaryDefinition } from '@/data/types'

type ValidationState = {
  code: string
  message: string
  missingDims: string[]
  sourceFact: string
  factVersion: number | null
  factGrain: string
}

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const validationError = ref<ValidationState | null>(null)
const definitionView = ref<SummaryDefinition | null>(null)
const queryPlan = ref<QueryPlan | null>(null)

const REASON_TEXT: Record<string, string> = {
  GRAIN_TOO_COARSE: '汇总粒度粗于请求粒度，细粒度查询不能错用',
  GRAIN_NOT_MATCH: '分组键与请求粒度不符',
  TIME_GRAIN_TOO_COARSE: '时间粒度粗于请求粒度',
  INVALID_DEFINITION: '汇总字段定义已失效',
}

function reasonText(reason: string): string {
  return REASON_TEXT[reason] ?? reason
}

type RowValidationMeta = {
  code: string
  valid: boolean
  missing_dims?: string[]
}

/** 从动态行里取出汇总校验戳记，并收窄成确定类型。 */
function rowValidation(row: EntryRow): RowValidationMeta | null {
  const validation = row._validation
  if (validation && typeof validation === 'object' && 'code' in validation) {
    return validation as unknown as RowValidationMeta
  }
  return null
}
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  validationError.value = null
  showCreate.value = true
}

function closeCreate() {
  showCreate.value = false
  validationError.value = null
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) {
      // 提交失败：保留全部输入，只展示错误，绝不重置或清空表单
      if (result.retained) {
        for (const [field, value] of Object.entries(result.retained)) {
          createValues.value[field] = String(value ?? '')
        }
      }
      const details = (result.details ?? {}) as Record<string, unknown>
      validationError.value = {
        code: result.code ?? 'VALIDATION_ERROR',
        message: result.message,
        missingDims: Array.isArray(details.missing_dims) ? (details.missing_dims as string[]) : [],
        sourceFact: String(details.source_fact ?? createValues.value['源事实表'] ?? ''),
        factVersion: typeof details.fact_version === 'number' ? details.fact_version : null,
        factGrain: String(details.fact_grain ?? ''),
      }
      return
    }
    showCreate.value = false
    validationError.value = null
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function viewDefinition(row: EntryRow) {
  errorMessage.value = ''
  try {
    definitionView.value = await loadSummaryDefinition(Number(row.id))
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '汇总定义读取失败'
  }
}

async function viewQueryPlan(row: EntryRow) {
  errorMessage.value = ''
  try {
    queryPlan.value = await loadQueryPlan(Number(row.id))
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '选源计划读取失败'
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>
