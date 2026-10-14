const accountRole = document.getElementById('id_account_role');
const playerFields = document.querySelectorAll('.admin-player-fields');

function updatePlayerFields() {
    const isPlayer = accountRole.value === 'player';
    playerFields.forEach((field) => {
        field.hidden = !isPlayer;
        const input = field.querySelector('input');
        if (input) input.required = isPlayer;
    });
}

accountRole.addEventListener('change', updatePlayerFields);
updatePlayerFields();
