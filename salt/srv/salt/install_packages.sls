{%- set packages = salt['pillar.get']('install_packages', {}) %}
{%- for package_name, package_version in packages.items() %}
{%- if package_version is not none %}
install_{{ package_name }}:
  pkg.installed:
    - name: '{{ package_name }}'
    - version: '{{ package_version }}'
{%- else %}
remove_{{ package_name }}:
  pkg.purged:
    - name: '{{ package_name }}'
{%- endif %}
{%- endfor %}
