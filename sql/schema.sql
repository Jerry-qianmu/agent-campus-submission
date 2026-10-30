-- ============================================================================
-- 素材审核与发布排期 Agent —— MySQL 设计（保留稿）
--
-- 本文件不参与运行：当前交付为 mock 模式，数据落 JSON 文件（见 src/store.py）。
-- 保留这份 DDL 的目的：
--   1. 记录真实部署时的表结构、唯一键与字段口径；
--   2. 让 src/store.py 的「表/唯一键/事务」有可对照的语义来源；
--   3. 迁移到 MySQL 时只需替换 repository 的驱动，业务代码无需改动。
--
-- 与 JSON 库的字段对应关系见 docs/06-implementation.md「JSON 库与 MySQL 的映射」。
-- ============================================================================

CREATE DATABASE IF NOT EXISTS `material_schedule`
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE `material_schedule`;

-- ---------------------------------------------------------------------------
-- 1. 素材库（真实环境为外部素材系统；mock 模式放在同一份 seed 里）
-- ---------------------------------------------------------------------------
CREATE TABLE `materials` (
  `id`         VARCHAR(64)  NOT NULL COMMENT '素材ID',
  `metadata`   JSON         NOT NULL COMMENT '素材元数据：标题/封面/时长/版权/标签等',
  `synthetic`  TINYINT(1)   NOT NULL DEFAULT 1 COMMENT '是否为合成数据',
  `create_time` DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time` DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='素材库（只读源）';

-- ---------------------------------------------------------------------------
-- 2. 素材信息审核（一素材一行，只存最新结论；历史流水见 material_review_log）
--    release_date 为素材发布时间，默认 NULL；NULL 表示未发布。
--    本期不接真实发布，因此该列恒为 NULL —— 它是「未发布」的结构性证据。
-- ---------------------------------------------------------------------------
CREATE TABLE `material_review` (
  `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `material_id`   VARCHAR(64)  NOT NULL COMMENT '素材ID',
  `status`        VARCHAR(16)  NOT NULL DEFAULT 'PENDING'
                  COMMENT 'PENDING/PASSED/INCOMPLETE/RISK/PUBLISHED',
  `metadata`      JSON         NULL COMMENT '审核时的元数据快照',
  `release_date`  DATETIME     NULL DEFAULT NULL COMMENT '实际发布时间，NULL=未发布',
  `risk_reason`   VARCHAR(512) NULL COMMENT '风险原因摘要，便于列表展示',
  `rules_version` VARCHAR(16)  NULL COMMENT '审核规则版本，便于回归定位',
  `create_time`   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time`   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_material` (`material_id`),
  KEY `idx_status` (`status`),
  KEY `idx_release_date` (`release_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='素材审核信息';

-- ---------------------------------------------------------------------------
-- 3. 发布排期（草稿）。status 上限为 CONFIRMED，没有任何代码路径写 PUBLISHED。
--    uk_material_date：同一素材同一时段只允许一行 —— 重复提交的第二份在库层就写不进去。
--    uk_idem        ：同一请求（同素材/同时段/同规则版本）幂等键唯一，收敛重试与连点。
-- ---------------------------------------------------------------------------
CREATE TABLE `publish_schedule` (
  `id`           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `date`         DATETIME     NOT NULL COMMENT '计划发布时段，整点对齐',
  `material_id`  VARCHAR(64)  NOT NULL COMMENT '素材ID',
  `status`       VARCHAR(16)  NOT NULL DEFAULT 'DRAFT'
                 COMMENT 'DRAFT=草稿待确认/CONFIRMED=用户已确认(仍是草稿)/CANCELLED=作废/PUBLISHED=仅外部系统回写',
  `confirm_time` DATETIME     NULL COMMENT '用户确认时间，NULL 表示待确认',
  `idem_key`     CHAR(64)     NULL COMMENT '幂等键 sha256(material_id|date|rules_version)',
  `create_time`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_material_date` (`material_id`, `date`),
  UNIQUE KEY `uk_idem` (`idem_key`),
  KEY `idx_date_status` (`date`, `status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='发布排期(草稿)';

-- ---------------------------------------------------------------------------
-- 4. 提醒日志：同一个 (material_id, event, payload) 只发一次，靠唯一键兜底。
--    提醒失败不回滚排期 —— 通知是旁路，不是主链路。
-- ---------------------------------------------------------------------------
CREATE TABLE `notify_log` (
  `id`          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `dedupe_key`  VARCHAR(128) NOT NULL COMMENT 'material_id:event:slot',
  `material_id` VARCHAR(64)  NOT NULL,
  `event`       VARCHAR(32)  NOT NULL COMMENT 'review_incomplete/review_blocked/confirm_needed/draft_saved',
  `channel`     VARCHAR(16)  NOT NULL DEFAULT 'inapp',
  `payload`     JSON         NULL,
  `status`      VARCHAR(16)  NOT NULL DEFAULT 'SENT' COMMENT 'SENT/FAILED',
  `create_time` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_dedupe` (`dedupe_key`),
  KEY `idx_material` (`material_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='提醒日志(幂等)';

-- ---------------------------------------------------------------------------
-- 5. （可选）审核流水：支撑「这次为什么判风险」的追溯与规则回归
-- ---------------------------------------------------------------------------
CREATE TABLE `material_review_log` (
  `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `material_id`   VARCHAR(64) NOT NULL,
  `status`        VARCHAR(16) NOT NULL,
  `reason`        VARCHAR(512) NULL,
  `rules_version` VARCHAR(16) NULL,
  `create_time`   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_material_time` (`material_id`, `create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='审核流水';
