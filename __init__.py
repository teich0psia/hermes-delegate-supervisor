"""Native directory-plugin entry point; pip loads the nested package directly."""
def register(ctx):
    from .hermes_delegate_supervisor import register as register_plugin
    return register_plugin(ctx)
