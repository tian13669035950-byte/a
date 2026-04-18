import re

def snake_to_camel(snake_str: str) -> str:
    """将 snake_case 字符串转换为 camelCase"""
    if "_" not in snake_str:
        return snake_str
    components = snake_str.split('_')
    return components[0] + ''.join(x.title() for x in components[1:])

def camel_to_snake(camel_str: str) -> str:
    """将 camelCase 字符串转换为 snake_case"""
    # 在大写字母前插入下划线，然后转为小写
    snake_str = re.sub('([a-z0-9])([A-Z])', r'\1_\2', camel_str).lower()
    return snake_str
