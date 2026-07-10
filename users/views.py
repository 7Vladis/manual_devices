from django.contrib.auth.views import LoginView
from .forms import LoginForm

class MyLoginView(LoginView):
    form_class = LoginForm
    template_name = 'authorization/login.html'

    def form_valid(self, form):
        remember_me = form.cleaned_data.get('remember_me')
        if not remember_me:
            # Сессия закроется при закрытии браузера
            self.request.session.set_expiry(0)
        return super().form_valid(form)