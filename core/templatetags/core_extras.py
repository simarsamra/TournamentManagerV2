from django import template

register = template.Library()


@register.filter
def dict_get(value, key):
    """Safely return dictionary value by key for Django templates."""
    if not isinstance(value, dict):
        return None
    return value.get(key)
