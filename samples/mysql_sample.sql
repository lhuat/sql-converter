-- 演示用 SQL（非生产数据）
DROP TABLE IF EXISTS `file_relation`;
CREATE TABLE `file_relation` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '主键',
  `file_id` bigint(20) unsigned NOT NULL,
  `biz_id` bigint(20) NOT NULL,
  `is_main` tinyint(1) NOT NULL DEFAULT 0,
  `created_at` datetime(6) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_file_biz` (`file_id`,`biz_id`),
  KEY `idx_biz_main` (`biz_id`,`is_main`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='关联表';
INSERT INTO `file_relation` (`id`,`file_id`,`biz_id`,`is_main`) VALUES (1,10001,20001,1),(2,10002,20002,0);
