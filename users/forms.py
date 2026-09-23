from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.password_validation import validate_password

User = get_user_model()

class LoginForm(AuthenticationForm):
    remember_me = forms.BooleanField(required=False, initial=True, label="Запомнить меня")
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Добавляем классы Bootstrap для стилизации
        self.fields['username'].widget.attrs.update({
            'class': 'form-control', 'placeholder': 'Email address'
        })
        self.fields['password'].widget.attrs.update({
            'class': 'form-control', 'placeholder': 'Password'
        })
        self.fields['remember_me'].widget.attrs.update({
            'class': 'form-check-input'
        })

class UserCreateForm(forms.ModelForm):
    """
    Создание локальной учётной записи из панели настроек.

    Раньше пользователь создавался напрямую из request.POST: настроенные
    AUTH_PASSWORD_VALIDATORS не вызывались (проходил любой слабый пароль),
    а повторный email приводил к IntegrityError и ответу 500.
    """

    password = forms.CharField(
        label="Пароль",
        widget=forms.PasswordInput(attrs={'class': 'form-control', 'placeholder': '••••••••'}),
        strip=False,
    )
    password_confirm = forms.CharField(
        label="Повторите пароль",
        widget=forms.PasswordInput(attrs={'class': 'form-control', 'placeholder': '••••••••'}),
        strip=False,
    )

    class Meta:
        model = User
        # Роль намеренно не редактируется при создании: новая учётная запись
        # всегда junior, повысить её можно отдельным действием в списке.
        fields = ['email', 'username']
        labels = {
            'email': "Email адрес",
            'username': "Имя пользователя",
        }
        widgets = {
            'email': forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'engineer@company.com'}),
            'username': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Иванов Иван Иванович'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['username'].required = False

    def clean_email(self):
        # Email — это USERNAME_FIELD, поэтому нормализуем регистр домена и
        # проверяем занятость до обращения к БД на запись.
        email = User.objects.normalize_email(self.cleaned_data['email'].strip())
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Пользователь с таким email уже зарегистрирован.")
        return email

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get('password')
        confirm = cleaned.get('password_confirm')

        if password and confirm and password != confirm:
            self.add_error('password_confirm', "Пароли не совпадают.")
            return cleaned

        if password:
            # Проверяем пароль настроенными валидаторами проекта.
            # user нужен для UserAttributeSimilarityValidator.
            candidate = User(email=cleaned.get('email') or '', username=cleaned.get('username') or '')
            try:
                validate_password(password, user=candidate)
            except forms.ValidationError as exc:
                self.add_error('password', exc)

        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data['email']
        user.username = (self.cleaned_data.get('username') or '').strip() or user.email.split('@')[0]
        user.role = 'junior'
        user.auth_source = 'django'
        user.set_password(self.cleaned_data['password'])
        if commit:
            user.save()
        return user
