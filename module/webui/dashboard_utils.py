"""
仪表盘 Scope ID 管理工具。

集中管理仪表盘相关的 PyWebIO scope 命名，避免重复创建和命名冲突。
对 group_name 进行清洗，确保生成的 scope_id 是有效的标识符。
"""

# Инструмент управления scope ID Dashboard
# Централизованно управляет именованием scope для dashboard, предотвращая повторное создание и конфликты имён

import re


class DashboardScopeManager:
    """
    Dashboard Scope ID 管理器
    
    用途：
    1. 统一管理 dashboard 相关的 scope 命名规则
    2. 对 group_name 进行清洗，确保生成的 scope_id 是有效的标识符
    3. 提供一致的前缀管理，方便将来修改
    """
    
    # Единый префикс scope для Dashboard
    DASHBOARD_PREFIX = "dashboard_"
    
    @classmethod
    def sanitize_group_name(cls, group_name: str) -> str:
        """
        清洗 group_name，移除或替换对 scope 标识符无效或不安全的字符
        
        Args:
            group_name: 原始的 group 名称
            
        Returns:
            清洗后的安全 group 名称
            
        Examples:
            >>> DashboardScopeManager.sanitize_group_name("My Group")
            'My_Group'
            >>> DashboardScopeManager.sanitize_group_name("Group-Name!")
            'Group_Name_'
            >>> DashboardScopeManager.sanitize_group_name("Group@#$Name")
            'Group___Name'
        """
        if not group_name:
            return "unknown"
        
        # Заменяем пробелы и специальные символы подчёркиваниями
        # Сохраняем буквы, цифры, подчёркивания и дефисы
        sanitized = re.sub(r'[^\w\-]', '_', group_name)
        
        # Не допускаем начало с цифры: при необходимости добавляем префикс
        if sanitized and sanitized[0].isdigit():
            sanitized = f"group_{sanitized}"
        
        # Удаляем повторяющиеся подчёркивания
        sanitized = re.sub(r'_+', '_', sanitized)
        
        # Удаляем подчёркивания в начале и конце
        sanitized = sanitized.strip('_')
        
        return sanitized or "unknown"
    
    @classmethod
    def get_dashboard_scope_id(cls, group_name: str) -> str:
        """
        生成 dashboard 的 scope ID
        
        Args:
            group_name: dashboard 组的名称
            
        Returns:
            完整的 scope ID，格式为 "dashboard_{sanitized_group_name}"
            
        Examples:
            >>> DashboardScopeManager.get_dashboard_scope_id("Oil")
            'dashboard_Oil'
            >>> DashboardScopeManager.get_dashboard_scope_id("My Group")
            'dashboard_My_Group'
        """
        sanitized_name = cls.sanitize_group_name(group_name)
        return f"{cls.DASHBOARD_PREFIX}{sanitized_name}"
    
    @classmethod
    def get_group_scope_id(cls, group_name: str) -> str:
        """
        生成 group 的 scope ID（用于 dashboard 内部的子 scope）
        
        Args:
            group_name: group 的名称
            
        Returns:
            group 的 scope ID，格式为 "{sanitized_group_name}_group"
            
        Examples:
            >>> DashboardScopeManager.get_group_scope_id("Oil")
            'Oil_group'
        """
        sanitized_name = cls.sanitize_group_name(group_name)
        return f"{sanitized_name}_group"
    
    # Набор зарегистрированных scope ID для обнаружения конфликтов
    _registered_ids: set = set()
    
    @classmethod
    def change_prefix(cls, new_prefix: str) -> None:
        """
        修改 dashboard scope 的前缀
        
        注意：此方法仅用于特殊情况，一般不建议在运行时修改
        
        Args:
            new_prefix: 新的前缀字符串
        """
        cls.DASHBOARD_PREFIX = new_prefix
    
    @classmethod
    def check_collision(cls, group_name: str) -> bool:
        """
        检测 scope ID 是否存在冲突（不同的 group_name 清洗后得到相同的 scope ID）
        
        Args:
            group_name: 原始的 group 名称
            
        Returns:
            True 表示存在冲突，False 表示无冲突
        """
        scope_id = cls.get_dashboard_scope_id(group_name)
        if scope_id in cls._registered_ids:
            return True
        cls._registered_ids.add(scope_id)
        return False
    
    @classmethod
    def reset_registry(cls) -> None:
        """重置已注册的 scope ID 集合（用于页面刷新时）"""
        cls._registered_ids.clear()


# Удобная функция для непосредственного импорта и использования
def get_dashboard_scope_id(group_name: str) -> str:
    """
    便捷函数：生成 dashboard 的 scope ID
    
    Args:
        group_name: dashboard 组的名称
        
    Returns:
        完整的 scope ID
    """
    return DashboardScopeManager.get_dashboard_scope_id(group_name)


def get_group_scope_id(group_name: str) -> str:
    """
    便捷函数：生成 group 的 scope ID
    
    Args:
        group_name: group 的名称
        
    Returns:
        group 的 scope ID
    """
    return DashboardScopeManager.get_group_scope_id(group_name)
